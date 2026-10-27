# CRM leads: adding a person directly to a company

**Status:** proposed, nothing implemented
**Decisions taken:** CRM-owned `crm_leads` collection · leads are NOT marketable until converted · duplicate policy recommended below

---

## Amendment — contact ownership comes first

A later requirement changes the premise of this document: **remove the current
platform-contact flow and give the CRM its own contacts.** `lead-phases.md` holds
the revised build order. Two sections here are superseded:

- **§2 "What the choice buys"** — `crm_leads` existed because the CRM could not
  own contacts. Once it owns them, a second CRM people table is duplication.
  The recommendation is now ONE `crm_contacts` collection with a
  `lifecycle_stage` field (`lead` → `qualified` → `customer`).
- **§11 "Conversion"** — mostly disappears. There is no cross-service
  conversion, no global-unique-email hazard, and no three-call UI sequence.
  Qualifying a lead becomes a `PATCH` on one field.

What still stands: the duplicate policy (§4, email optional, warn-and-offer),
the association-enum trap (§5), the attribute and note/task reasoning (§6, §7),
the contract constants that move (§10), and the whole risk register (§14) —
except that `contact_scope.py` and its class of tenancy bug leave the CRM
entirely with this change, which is the strongest argument for the new
direction.

---

## 1. The problem, stated precisely

Today a company's People tab is built entirely from **platform contacts**. The
chain is:

```
crm_associations (company ↔ contact)
  → foreign.contacts_by_ids()
    → host/directory.py → the platform's `contacts` collection
```

There is no way to put a person on a company unless that person already exists
as a platform contact. For a sales team that is backwards: the first thing you
know about a lead is a name and a company, and you want it recorded before it
is anything else.

Two constraints make this harder than it looks, and both are already documented
in the codebase:

**`foreign.py` is read-only by design.** Its docstring is explicit — *"There is
no `write_*` function here, and there should never be one. A write to a host
collection is a request to the host, not a query."* The CRM owns 14 collections
and writes to no others.

**`contacts.email` is globally unique across every account.** Not per-account —
globally. `app/services/contact_scope.py` exists entirely because of this, and
says what it costs: *"a path that looks a contact up in order to create one if
it is missing cannot be scoped: scoping the read makes it miss another
account's row, the insert then violates the index."*

So "just create a contact from the CRM" would mean the CRM writing to a host
collection **and** taking on a cross-tenant unique-index hazard it currently has
no code for. The chosen design avoids both.

---

## 2. What the choice buys

A new CRM-owned collection means:

- No write to the host. `foreign.py` stays five read-only delegations.
- No platform change at all for the core feature.
- No global uniqueness problem — `crm_leads.email` is scoped per account like
  every other `crm_*` collection, via the existing `assert_account_scoped`
  wired into the CRUD base.
- Leads are structurally invisible to campaigns, segments and automations,
  because those read `contacts` and a lead is not in `contacts`. The
  "not marketable until qualified" requirement is enforced by the schema, not
  by a flag every send path has to remember to honour.

That last point is the strongest argument for this shape. The flagged-contact
alternative needs every bulk-send path to respect a marker, and one that forgets
leaks sales leads into a marketing blast. Here it cannot happen.

The cost is that a company's People tab now has **two sources**, and that is
where most of the real work is (§8).

---

## 3. Data model

New collection, `crm_leads`, as the 15th CRM collection.

```python
class CrmLeadModel(Document):
    created_by: PyObjectId              # tenancy — same as every crm_* model

    first_name: str
    last_name: Optional[str] = None
    email: Optional[str] = None         # OPTIONAL — see §4
    phone: Optional[str] = None
    job_title: Optional[str] = None

    status: str = "new"                 # new | working | qualified | unqualified | converted
    source: Optional[str] = None        # "manual" | "import" | "web_form" | ...
    owner_id: Optional[PyObjectId] = None

    custom_fields: Dict[str, Any] = {}  # the existing attribute system, §6

    # Set only by conversion. Non-null means this lead became a contact.
    converted_contact_id: Optional[PyObjectId] = None
    converted_at: Optional[datetime] = None

    created_at / updated_at / created_by_user_id
```

Indexes:

| Index | Why |
|---|---|
| `(created_by, email)` **partial, unique**, `email` non-null | per-account uniqueness without blocking the many leads that have no email |
| `(created_by, created_at DESC)` | the default list ordering |
| `(created_by, owner_id)` | "my leads" |
| `(created_by, status)` | pipeline-style filtering |

**No company_id field.** A lead's company link goes in `crm_associations` like
everything else — the join-collection pattern the CRM already uses for
company↔contact, company↔deal and deal↔contact. Putting a `company_id` on the
lead would be a second way to express the same relationship, and the
association model's own docstring explains why that class of duplication was
rejected the first time.

---

## 4. Duplicate handling — my recommendation

You asked me to recommend this one. **Warn and offer, defaulting to link** — not
silent linking, and not free-for-all duplicates.

The reasoning:

**Email must be optional.** A phone-first sales team records "Priya at Acme,
mobile only" constantly. Making email required to create a lead means people
type `x@x.com` to get past the form, which is worse than no email.

**But when email IS given, check it — against both tables.** The dangerous case
isn't lead-duplicates-lead, it's a rep creating a lead for someone who already
exists as a marketing contact. They then work a record that has no campaign
history, no consent state, and no connection to the real person. Check
`crm_leads` (scoped) and, via the existing read-only
`foreign.contact_by_email`, the platform contacts.

**Offer, don't auto-link.** Silently attaching an existing contact to a company
because the email matched is a surprise, and "same email, different person"
genuinely happens with shared inboxes (`info@`, `sales@`). Show what was found
and let the user choose:

```
A contact with this email already exists:
  Priya Sharma · priya@acme.com · contact since Mar 2026
  [ Link Priya to Acme Corp ]   (recommended)
  [ Create a separate lead anyway ]
```

**And the part that matters most:** if the directory lookup is *unavailable*,
the check must fail loudly — never fall through to "no duplicate found".
`host/directory.py` already draws exactly this line and explains the
consequence: *"turning 'the host is down' into 'no such contact' [means] a
thirty-second outage"* produces duplicate people. `contact_by_email` is a
LOOKUP and raises `DirectoryUnavailable`; the create-lead handler must let that
propagate as a 503, not catch it and proceed. This is the single most likely way
to reintroduce the duplicate-people bug, so it needs a test that stubs the
directory as unavailable and asserts **no lead was written**.

---

## 5. Associations — one real trap

`AssociationEntity` needs a `LEAD` member. Its docstring is a warning:

> *"Ordering here is meaningful: it defines the canonical direction a pair is
> stored in. Reordering these members changes the storage direction and would
> orphan existing rows — don't."*

So **append**, never insert:

```python
class AssociationEntity(str, Enum):
    COMPANY = "company"
    DEAL = "deal"
    CONTACT = "contact"
    LEAD = "lead"        # APPENDED. Inserting it earlier would silently
                         # re-point the canonical direction of every existing
                         # company↔contact and deal↔contact row.
```

Lead pairs then normalise as `company→lead` and `deal→lead`, consistent with
the existing precedence. The unique index keeps working because normalisation
still happens before every write.

Worth a test that asserts the enum's member *order*, not just membership — a
future alphabetical tidy-up of that class is a silent data corruption, and the
only thing standing between you and it is currently a comment.

---

## 6. Custom attributes

`CrmObjectType` gains `LEAD = "lead"`, and leads get the same
typed-column-plus-`custom_fields` treatment companies and deals have. This is
mostly free: `attribute_routing` already routes custom values generically, and
`AttributeRenderer` on the UI side already handles all 13 attribute types.

One thing to decide: should a lead's attributes be **its own set**, or shared
with `contact`? I'd give leads their own `object_type`. Sharing means a renamed
contact attribute silently rewrites lead data, and converting a lead would need
the two sets to agree.

---

## 7. Notes, tasks and activity

`CrmNoteModel` and `CrmTaskModel` both carry `contact_ids: List[PyObjectId]`. A
lead id dropped into that array would be indistinguishable from a contact id —
same type, same shape, different collection. That is a silent wrong-join
waiting to happen.

Add a parallel field rather than overloading:

```python
contact_ids: List[PyObjectId]   # platform contacts, unchanged
lead_ids:    List[PyObjectId]   # NEW — crm_leads
```

Plus matching indexes (`notes_by_lead`, `tasks_by_lead`) mirroring the existing
`notes_by_contact` / `tasks_by_contact`.

More fields, but the alternative is a single array whose entries mean different
things depending on a sibling column, and every reader has to get that right
forever.

---

## 8. The People tab — the actual hard part

A company's People tab becomes a union of two collections. Merging across two
sources breaks naive pagination: you cannot `skip`/`limit` a union without
over-fetching from both and discarding, and the result is unstable as rows
change.

Three options, in order of how much I'd recommend them:

**A. Two separate sections on the page.** "Contacts (4)" and "Leads (7)", each
paginated independently against its own collection. No merge, no union query,
no new endpoint shape — `GET /crm/companies/{id}/contacts` keeps working
untouched and a sibling `GET /crm/companies/{id}/leads` appears beside it.
Honest about the fact that these are different kinds of record, which the user's
own requirement says they are. **This is what I'd build.**

**B. One merged list, server-side.** One endpoint returns both kinds with a
`kind` discriminator, sorted together. Needs over-fetch-and-trim, and a stable
sort key present in both collections (`created_at` works). Gets expensive on a
company with thousands of people, and "total" becomes two counts added
together, which is fine until someone filters.

**C. One merged list, client-side.** The UI fetches both and interleaves. Fine
for a detail page capped at ~50 each; falls apart with a "load more".

Option A also sidesteps a question B forces immediately: when a merged row is
clicked, does it open a contact detail page or a lead detail page? Two sections
make that obvious.

---

## 9. API surface

A new `app/routes/leads.py`, 8 endpoints:

| Method | Path | Notes |
|---|---|---|
| `GET` | `/crm/leads` | list; filters on status, owner, source, company |
| `POST` | `/crm/leads` | create; runs the duplicate check in §4 |
| `GET` | `/crm/leads/{id}` | detail, with expanded company/owner |
| `PATCH` | `/crm/leads/{id}` | partial update |
| `DELETE` | `/crm/leads/{id}` | soft or hard — match whatever companies does |
| `POST` | `/crm/leads/{id}/links` | attach company / deal (additive, idempotent) |
| `DELETE` | `/crm/leads/{id}/links` | detach (subtractive, never a replace) |
| `POST` | `/crm/leads/{id}/convert` | §11 |

Plus 1 on the companies router: `GET /crm/companies/{id}/leads` (option A above).

Both response envelopes are already fixed by the codebase and must be matched
exactly — single `{status, message, data}`, list `{data, total, status}` — and
business failures are HTTP 200 with `status: "failure"`, not an error status.

---

## 10. The contract constants that will break

This is the part most likely to be missed, because the failures are loud but far
from the change. Adding one router moves all of these:

| Constant | File | Now | After |
|---|---|---|---|
| `EXPECTED_ROUTERS` | `app/main.py:38` | 12 | **13** |
| `EXPECTED_ENDPOINTS` | `app/main.py:39` | 83 | **92** |
| `ALL_MODULES` | `app/profiles.py` | 12 entries | +`leads` |
| `core` profile | `app/profiles.py` | 61 endpoints / 7 modules | decide if leads are core |
| `minimal` profile | `app/profiles.py` | 47 / 5 | probably not |
| mount count | derived | 16 | **17** |

And `scripts/check_crm_contract.py` parses **literal** `include_router` calls
via AST — a loop broke it once already. The new mount must be a literal call
inside the profile gate, in the same `if "leads" in modules:` shape as the
others.

`/health` reports `routers: 13/13` and `endpoints: 92/92` when this is right,
which is the fastest way to confirm the whole wiring landed.

Decide deliberately whether `leads` belongs in the `core` profile. I'd say yes —
a CRM you can't add a person to is not a core CRM — which makes `core` 69/8.

---

## 11. Conversion: lead → platform contact

This is where the ownership line finally has to bend, and where the global
unique email bites. **I'd recommend keeping the CRM out of it entirely.**

Orchestrate conversion from the **UI**, in three calls:

```
1. UI  → POST /api/v1/contacts            (platform, already exists)
         creates the real contact, through the platform's own ingestion
         path — which already handles the global-unique-email collision
         via find_by_email_global + may_write

2. UI  → POST /crm/companies/{id}/links   (CRM, already exists)
         attaches the new contact to the company

3. UI  → POST /crm/leads/{id}/convert     (CRM, new)
         body: { contact_id }
         stamps converted_contact_id + converted_at, status="converted",
         and re-points the lead's notes/tasks at the contact
```

Why this way:

- `foreign.py` stays read-only. No `write_*`, no new internal endpoint, no
  second implementation of the platform's contact-creation rules.
- The global-uniqueness hazard is handled by the code that already handles it.
  A naive `contacts.insert_one` from the CRM would hit the unique index on
  another tenant's row, and the CRM has no `may_write` equivalent.
- Consent and opt-in are captured at the moment of conversion, by the platform
  path that owns them. This is the point where "not marketable" becomes
  "marketable", and it should go through the front door.

The CRM's `/convert` endpoint deliberately does **not** create the contact. It
records that conversion happened and takes the `contact_id` on trust, after
verifying via the read-only `foreign.contact_by_id` that the id exists and
belongs to this account. If that lookup is unavailable, refuse — same rule as
§4.

A converted lead is kept, not deleted. It is the audit trail of where the
contact came from, and deleting it would orphan its notes and activity.

---

## 12. UI changes (`emailMarketingUI`)

- **Company detail** — a "Leads" section beside "Contacts", with **+ Add lead**.
- **Add-lead dialog** — name required, email and phone optional, company
  pre-filled from context, plus the duplicate-warning state from §4.
- **Lead detail page** — mirrors the company/deal detail layout; reuses
  `AttributeRenderer` for custom fields, so it needs no new field widgets.
- **Leads list screen** — new route + sidebar entry. Reuse the saved-views and
  `FilterBuilder` machinery rather than growing a second filter UI.
- **Convert button** — drives the three-call sequence in §11, and must be
  idempotent in the face of a half-failure: if step 1 succeeds and step 3 fails,
  the contact exists and the lead still says "new". Show the partial state
  rather than silently retrying step 1, which would create a second contact.
- All CRM calls route through the existing `/crm/` path prefix in
  `axiosInstances.js`, so no routing change is needed.

---

## 13. Suggested phasing

Each phase ships and is useful on its own.

| Phase | Scope | Why this boundary |
|---|---|---|
| **1** | Model, indexes, enums, CRUD, the 8 + 1 endpoints, contract constants | Backend complete and testable with no UI. `/health` confirms 92/92. |
| **2** | Company detail "Leads" section + add-lead dialog, incl. duplicate warning | The feature you actually asked for, end to end. |
| **3** | Lead detail page, leads list screen, saved views | Makes leads a first-class object rather than a company sub-list. |
| **4** | Conversion | Deliberately last — it is the only part that crosses the service boundary, and it is easier to design once real lead data exists. |
| **5** | Custom attributes for `object_type = "lead"`; notes/tasks `lead_ids` | Additive; don't let it block phase 2. |

---

## 14. Risks, and the specific ones for this codebase

**The duplicate check failing open.** Covered in §4. Highest-consequence risk
here, and the codebase has already paid for this exact mistake once.

**Two copies of one rule.** This repo's recurring bug — the `currencyCode`
duplicate, the four copies of the host defaults, the two coercion maps, the two
system-attribute sets. Leads will tempt you into a second copy of status
vocabularies, a second lead-shaped serializer, a second normalisation helper.
Every rule leads share with contacts should be imported, not retyped.

**The enum ordering.** §5. A silent data corruption guarded only by a comment.

**Pagination across two sources.** §8. Choosing option A avoids it entirely;
options B and C both have an over-fetch cost that only shows up on real data.

**Guards that match their own documentation.** Five instances so far in this
repo, most recently the `compare_digest` source grep. Any test asserting "the
duplicate check calls `contact_by_email`" must assert it on the **AST or on
behaviour**, not by grepping the source for the name — the docstring will
contain the name.

**Attribute set sharing.** §6. Sharing `contact` attributes with leads looks
like a saving and makes conversion and renames hazardous.

---

## 15. What I'd want decided before coding

1. **Option A, B or C for the People tab** (§8) — I recommend A.
2. **Is `leads` in the `core` profile?** (§10) — I recommend yes.
3. **Delete semantics** — soft or hard, matching whichever companies uses.
4. **Status vocabulary** — the `new | working | qualified | unqualified |
   converted` list above is a guess; it should be yours, and it should live in
   `settings` alongside `lost_reasons` if you want it editable per account.
