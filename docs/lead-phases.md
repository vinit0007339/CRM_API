# CRM contacts & leads — phase-wise implementation plan

**Supersedes the phase order in the first draft.** The new requirement — *remove
the current contact flow and give the CRM its own contacts* — reverses the
build order. Contact ownership comes first; leads become a small addition on
top of it rather than a parallel system.

Design reasoning lives in `lead-plan.md`; §2 and §11 of that document are
amended by this one (see `lead-plan.md` → *Amendment*).

**Decisions taken:** CRM owns its contacts · leads are not marketable until
qualified · duplicates warn-and-offer with email optional.

---

## ⚠️ AMENDMENT: THERE IS NO BACKFILL

Phase 2's migration was **declined**, and `scripts/backfill_crm_contacts.py` is
deleted. Everything below that assumes it — phase 2 itself, phase 3's
`contacts_source` switch, phase 4b's shape — is superseded by the section
**"No backfill: the `crm_contact` association type"** at the end of this
document. Read that first; the phase write-ups are kept as the record of how
the decision was reached.

The short version: `AssociationEntity` gained a `crm_contact` member, so an
association row states which collection its contact id belongs to. Existing
rows keep resolving against the platform, new CRM contacts link as
`crm_contact`, and no data moves.

---

## The recommendation that changes everything

> **One collection, not two.** `crm_contacts` with a `lifecycle_stage` field
> (`lead` → `qualified` → `customer`), not `crm_leads` *and* `crm_contacts`.

In the first draft, `crm_leads` existed because the CRM could not own contacts —
contacts belonged to the platform, so a CRM-native person needed somewhere else
to live. Once the CRM owns its contacts, that reason is gone, and two CRM-owned
people tables would be the duplication this codebase keeps paying for: two
serializers, two sets of association rules, two duplicate-check paths, two
places a custom attribute can be defined, and a "convert" step that is really
just a row move.

A lead becomes `lifecycle_stage = "lead"`. Qualifying is a field update, not a
migration. The whole of the old phase 4 (conversion) disappears.

This is **decision #1** at the end of this document. Everything below assumes
it; say so if you'd rather keep two collections and I'll re-cut the phases.

---

## What "remove the current contact flow" actually touches

Smaller than it looks. The association plumbing doesn't care which collection
an id lives in — only the *expansion* does. Four real call sites:

| Site | What it does today |
|---|---|
| `app/routes/companies.py:404` | `_fetch_contacts` → the company Contacts tab |
| `app/services/import_worker.py:544` | `contact_by_email` — "does this person exist?" |
| `app/services/import_worker.py:547` | `contact_by_id` |
| `app/services/auto_link_service.py:88` | links `company → contact` using a platform contact id |

Plus the machinery behind them:

- `app/foreign.py` — three of its five delegations
- `app/host/directory.py` — `contact_by_email` / `contact_by_id` /
  `contacts_by_ids` on the `Directory` protocol and all three adapters
- `app/services/contact_scope.py` — **becomes entirely dead** (see below)
- `app/routes/internal.py` — `POST /api/v1/internal/crm/auto-link`, the
  platform's way in
- `emailMarketingAPI/api/routes/contacts.py:153,775` — `_auto_link_company` →
  `crm_client.link_one`

### The best consequence

`contact_scope.py` exists **only** because `contacts.email` is `Indexed(unique=True)`
globally, across every account. Its own docstring lists what that costs: a CSV
import overwriting another account's contact names, a webhook re-subscribing
somebody a different account unsubscribed, an SMS import rewriting another
account's consent.

A CRM-owned collection is scoped by `created_by` like every other `crm_*`
collection, through the `assert_account_scoped` already wired into the CRUD
base. So:

- the global-uniqueness hazard disappears
- `find_by_email_global` / `may_write` / the audited "reasons" list are not
  needed on the CRM side
- the conversion risk that made old phase 4 delicate evaporates

That module and its whole class of bug leave the CRM with this change. Worth
saying out loud, because it is the strongest argument for the direction you've
chosen.

---

## Phase order at a glance

| Phase | Ships | Endpoints | Routers | Reversible? |
|---|---|---|---|---|
| **0** | Decisions + three guard tests | 83 | 12 | n/a |
| **1** | `crm_contacts` model + CRUD, written by nothing yet | 83 | 12 | yes |
| **2** | Backfill: platform contacts → `crm_contacts`, ids preserved | 83 | 12 | yes (drop collection) |
| **3** | Cut the reads over; platform path still present | 83 | 12 | yes (one flag) |
| **4** | Remove the platform-contact flow, both repos | 83 | 12 | **no** |
| **5** | Contacts API — add a person directly to a company | **91** | **13** | yes |
| **6** | UI: company page, contact detail, contacts list | 90 | 13 | yes |
| **7** | `lifecycle_stage` — leads | 90 | 13 | yes |
| **8** | Custom attributes, notes & tasks | 90 | 13 | yes |

Phase 4 is the one-way door. Phases 1–3 are additive and leave the platform
path intact, so the cutover is a decision you make once you can see CRM
contacts working beside the old ones.

Note the endpoint count only moves **once**, at phase 5, and lands at 91 rather
than the 92 of the first draft — because merging leads into contacts removes the
separate leads router and the `/convert` endpoint.

Removing `POST /api/v1/internal/crm/auto-link` does **not** change
`EXPECTED_ENDPOINTS`: `NON_CRM_MODULES` already keeps the internal router out of
that count, which is why it is 83 and not 84.

---

## Phase 0 — decide, and plant three guards

Half a day. No feature code. Three guards, because phase 1 and phase 7 both
touch things nothing currently protects.

### 1. The association enum ordering

`AssociationEntity`'s docstring warns that reordering members "would orphan
existing rows". Nothing enforces it. Pin the sequence:

```python
assert [m.value for m in AssociationEntity] == ["company", "deal", "contact"]
```

### 2. The second copy of that ordering — a real find

`app/services/associations.py:26` holds an independent copy:

```python
# Lower number wins the "from" slot.
_PRECEDENCE = {"company": 0, "deal": 1, "contact": 2}
```

A stdlib-only module, deliberately no imports — so it cannot read the enum, and
the two will drift. **If phase 7 adds `LEAD` to the enum and not here,
`normalize_pair` raises `InvalidAssociation` on every lead pair** and the unique
index that depends on normalisation stops meaning anything.

Add a test that asserts the two agree:

```python
assert list(_PRECEDENCE) == [m.value for m in AssociationEntity]
```

Sixth instance of two-copies-of-one-rule in this repo. Catching it before it
costs anything is the cheapest it will ever be.

### 3. The RBAC permission set

`app/auth/rbac.py` is a copy of the platform's. Phase 5 adds `contacts:*`
handling on the CRM side; if the two copies disagree, `builtin_overrides`
returning from introspection can silently contradict what the CRM thinks a role
means. Pin the full `PERMISSIONS` set so a change there is chosen, not drifted.

### Acceptance

All three tests pass, **and each fails when you break what it guards.** Reorder
the enum, drop a `_PRECEDENCE` entry, remove a permission. A guard that survives
its own mutation is not a guard — this repo has produced five of those, the
latest a `compare_digest` check that was reading its own docstring.

---

## Phase 1 — `crm_contacts`, written by nothing

Additive and invisible. The collection exists; no code path uses it yet.

### New files

| File | Contents |
|---|---|
| `app/models/contact.py` | `CrmContactModel` + indexes |
| `app/crud/contact.py` | `CrmContactCrud` on `app/crud/base.py` |
| `app/services/contact_rules.py` | duplicate decision, stage transitions |

### The model

```python
class CrmContactModel(Document):
    created_by: PyObjectId                 # tenancy, as every crm_* model

    first_name: str
    last_name: Optional[str] = None
    email: Optional[str] = None            # OPTIONAL
    phone: Optional[str] = None
    job_title: Optional[str] = None

    lifecycle_stage: str = "lead"           # lead | qualified | customer  (phase 7)
    source: Optional[str] = None            # manual | import | platform_backfill | ...
    owner_id: Optional[PyObjectId] = None

    custom_fields: Dict[str, Any] = {}      # phase 8

    # Set by the phase-2 backfill. Non-null means this row mirrors a platform
    # contact, and is how you trace one back after the cutover.
    platform_contact_id: Optional[PyObjectId] = None

    created_at / updated_at / created_by_user_id

    class Settings:
        name = "crm_contacts"
```

| Index | Why |
|---|---|
| `(created_by, email)` **unique, partial** on non-null `email` | per-account uniqueness; the many phone-only rows don't collide |
| `(created_by, created_at DESC)` | default list order |
| `(created_by, owner_id)` | "my contacts" |
| `(created_by, lifecycle_stage)` | lead filtering, phase 7 |
| `(created_by, platform_contact_id)` | backfill idempotency, phase 2 |

**No `company_id` field.** The company link is a `crm_associations` row, like
company↔deal and deal↔contact. A second way to express one relationship is what
the association model's docstring says was rejected the first time.

### Also

Register `CrmContactModel` in `app/db.py`'s `MODELS`, or Beanie never builds
those indexes. `/health/ready` reports `models.declared`, so the count moving
from 14 to 15 is your confirmation.

### Tests

CRUD round-trip · tenancy isolation · email optional · the partial unique index
in all three cases (two null emails coexist; same email same account rejected;
same email different accounts allowed).

### Acceptance

`/health/ready` shows 15 declared models. `db.crm_contacts.getIndexes()` shows
five. Nothing else in the app behaves differently.

---

### What phase 1 actually did — APPLIED

Restored from `e1d22e9^` rather than written fresh, and it brought back more
than the plan designed:

| File | Lines | Note |
|---|---|---|
| `app/models/contact.py` | 123 | `lifecycle_stage`, rollups, denormalised `full_name` / `primary_company_name`, `tag_ids`, `custom_fields` |
| `app/crud/contact.py` | 67 | on the existing `CrmCrudBase` |
| `app/schemas/contact.py` | 122 | |
| `app/services/contact_rules.py` | 268 | came across **verbatim** — already pure logic |
| `app/routes/contacts.py` | 554 | **8** endpoints, not the 7 estimated |
| `app/crud/__init__.py` | +2 | exports `CrmContactCrud` |
| `app/db.py` | +1 | `CrmContactModel` registered **last**, as the monolith's registry had it |

The router has `/filter-fields`, `/bulk` and `/{id}/activities` that the plan
did not anticipate, which is why phase 5 is 91 endpoints and not 90.

**Open decision 4 is answered by the restored code.** `contact_rules.py:37`:

```python
LIFECYCLE = ("subscriber", "lead", "qualified", "customer", "churned")
```

Use that, not the guess earlier in this document. `lifecycle_stage` is a plain
field, so the merged-collection recommendation needs no new model work at all —
phase 7 is a stage filter and a badge.

**One line was corrected, not restored.** The model declared
`unique=True, sparse=True` on `(created_by, email)`, with a comment saying it
was so a phone-only contact could have no email. `sparse` does not do that on a
compound index — Mongo only skips a document when *all* indexed fields are
missing, and `created_by` never is, so the second phone-only contact in an
account collided on `(account, null)`. Now
`partialFilterExpression={"email": {"$type": "string"}}`. This matters directly:
email-optional is a decision already taken.

**Therefore a new prerequisite for any environment that ever had the
collection.** `crm_contacts` was never dropped, so the old
`uniq_crm_contact_email` is still on it, and creating the partial one under the
same name fails with `IndexOptionsConflict`. `app/db.py` sets
`allow_index_dropping=False` deliberately, so Beanie will not resolve it.
Readiness then reports `models.registered: false` for ever, with the real cause
one line in the startup log.

Run **`scripts/check_crm_contact_index.py`** before deploying. Report-only by
default; `--apply` issues one `drop_index` and nothing else, and refuses when
duplicate `(account, email)` pairs would stop the new index building anyway.

**Two checker changes came out of this**, both in
`scripts/check_crm_contract.py`:

1. It compared the contract against every route file on disk, so the restored
   router read as 8 undocumented endpoints and the run failed — while those
   endpoints were unreachable and documenting them would have made the contract
   lie. It now reports *defined but not mounted* separately and does not fail
   on it. Removing a real mount is still caught, because the contract's entries
   for it become phantoms.
2. Fixing that exposed a latent bug: the alias regex required `settings as
   crm_settings` to be alone on its line, and in `app/main.py` it is inline
   among other names. So `settings` never resolved to a mount prefix — invisible
   only because the guessed `/crm/<module>` fallback happened to be right for
   that one module. Alias resolution is now AST-based, as the file's own
   docstring says it should be.

### Acceptance — MET

```
/health      routers 12/12 · mounts 16 · endpoints 83/83
             contacts NOT mounted (only the pre-existing
             /companies/{id}/contacts appears)
models       15 declared, CrmContactModel last
contract     PASS, with 8 routes reported defined-but-not-mounted
invariants   PASS
```

Nothing committed.

---

## Phase 2 — backfill, with ids preserved

The migration, and the one design choice that makes phase 3 nearly free.

### The key idea

Existing `crm_associations` rows carry `to_type="contact"` with **platform
contact `_id`s**. Copy each referenced platform contact into `crm_contacts`
**keeping the same `_id`**. Then every association row already points at the
right CRM contact and needs no rewrite at all.

The alternative — new ids plus an association remap — means a second migration
over a join table, with a half-migrated state that is hard to reason about. Not
worth it.

```
for each distinct to_id where to_type = "contact", per account:
    read the platform contact  (read-only, existing foreign path)
    upsert into crm_contacts with _id = that same _id
        source = "platform_backfill"
        platform_contact_id = that same _id
        lifecycle_stage = "customer"   # it was a real contact, not a lead
```

### Properties it must have

**Idempotent.** Upsert on `_id`. Running it twice is a no-op; this script will
be run more than once.

**Resumable.** A batch of 50,000 will be interrupted. Log progress and skip
rows already present.

**Fails loudly, never partially-silently.** If the directory read is
unavailable, stop with a non-zero exit and a count of what was done. The
`step-c-remove-crm.sh` failure earlier in this project is the pattern to avoid:
`set -e` inside a silent loop left the tree half-changed and said nothing.

**Reports orphans rather than skipping them.** An association pointing at a
platform contact that no longer exists is real data to decide about, not a row
to drop quietly. Print them; don't delete them.

### Deliverable

`scripts/backfill_crm_contacts.py`, with `--dry-run` printing the counts it
would write per account, and a reconciliation mode that re-reads afterwards and
asserts every `to_type="contact"` association resolves.

### Acceptance

Dry run on a copy of production data. Then: for every account, the number of
distinct contact ids in `crm_associations` equals the number of
`crm_contacts` rows with `source = "platform_backfill"`. Zero unresolved
associations. Run it twice; the second run writes nothing.

---

### What phase 2 actually did — SCRIPT WRITTEN, NOT RUN

`scripts/backfill_crm_contacts.py`. Report-only by default; `--apply` writes,
`--reconcile` verifies. It upserts into `crm_contacts` and **writes nothing
else** — `crm_associations` and the platform `contacts` collection are
read-only to it, and it deletes nothing anywhere.

**It refuses to write in two cases, and both are structural rather than
unlucky.**

*One platform id wanted by two accounts.* A platform contact is ONE document
per email with an `owner_ids` list, shared between accounts. `crm_contacts` is
per-account. So when two accounts link the same platform contact, both want a
row under that one `_id` — and `_id` is unique per collection, so only one can
have it. This is not an edge case to wave at: it is the direct consequence of
the design that made a per-account CRM contact necessary. Resolving it means
choosing which account keeps the id and remapping the other's association rows
— a different migration, with a different risk profile, that must not ride
along inside this one.

*Normalised email collisions inside one account.* `crm_contacts` stores email
through `contact_rules.normalize_email` and its unique index is on that value.
`Contact.email` is deliberately NOT lowercased on write, so `Priya@Acme.com`
and `priya@acme.com` are two legitimate platform rows that fold to one value.
If one account links both, the second insert violates the partial index.
Caught before anything is written rather than halfway through.

**Two further things it reports but does not act on:** an association pointing
at a platform contact that no longer exists, and one whose account cannot
actually SEE the contact (neither `created_by` nor in `owner_ids`). The second
is the dangerous one — copying it would move one tenant's person into
another's CRM — so it is skipped and listed individually.

**`custom_fields` are deliberately not copied.** Platform custom fields are
keyed by the platform's attribute definitions; `crm_contacts.custom_fields` are
keyed by `crm_attributes`. Copying them would store values under keys nothing
in the CRM defines — invisible in the UI, undeletable through it, and
indistinguishable from real data later. The count is reported so the loss is a
known quantity.

**Lifecycle stage defaults to `subscriber`, not `customer`.** Everyone this
script touches got into the CRM by being in the marketing audience and linked
to a company, which is what `subscriber` means in `contact_rules.LIFECYCLE`.
`customer` would assert a commercial relationship the data does not show, and
a stage nobody trusts is a filter nobody uses. `--stage` overrides it.

**A re-run cannot clobber CRM work.** The write is `$set` plus `$setOnInsert`,
not a replace: `owner_id`, `lifecycle_stage`, `custom_fields` and `created_at`
are written only on insert, so an owner assigned or a stage moved on after the
first pass survives the second.

#### Verified — 25 checks against an in-memory Mongo

`mongomock_motor`, which is faithful for document shape, upsert semantics and
`$setOnInsert` — but **not** authoritative about index enforcement, so the
email-collision case is asserted on the script's own refusal rather than on a
duplicate-key error.

```
shared platform id across two accounts      DETECTED
case-folded email collision                 DETECTED
from_type='contact' (un-normalised legacy)  DETECTED
_id preserved · email normalised · full_name derived
custom_fields not copied · source stamped · created_at kept
multi-company leaves primary_company_id unset
single company resolved, name denormalised
visibility predicate carries BOTH halves
re-run preserves owner, stage and custom values
association's account wins over platform created_by
MUTATION: protected fields moved into $set -> owner WIPED
```

The last line is the point: the mutation shows the `$setOnInsert` split is
load-bearing and not decoration.

**Two bugs found by verifying rather than assuming.** `COMPANIES` was
`"crm_companies"`; the collection is actually `companies` — every other CRM
collection carries the prefix and this one does not. A `find_one` against a
non-existent collection returns `None`, so every `primary_company_name` would
have come out blank and read as missing source data. And `--reconcile` raised a
raw pymongo traceback on an unreachable database where the main path printed
one line naming the host; a verification tool whose failure output is a stack
trace gets read as "the migration is broken".

**Not run against your data.** This sandbox cannot reach Atlas, so the real
numbers — how many ids are shared across accounts, how many emails fold
together — are still unknown. Both refusals exist because those counts could
be non-zero. Run `--dry-run` style (no flags) on stage first.

#### Order of operations

```
1. python3 scripts/check_crm_contact_index.py          # phase 1's prerequisite
2. python3 scripts/backfill_crm_contacts.py            # report; resolve refusals
3. python3 scripts/backfill_crm_contacts.py --apply
4. python3 scripts/backfill_crm_contacts.py --reconcile
```

Step 4 must report zero unresolved before phase 3 repoints the reads.

---

## Phase 3 — cut the reads over

Still reversible. The platform path remains in the code; the CRM simply stops
reading through it.

### Changes

| File | From | To |
|---|---|---|
| `app/routes/companies.py:404` | `foreign.contacts_by_ids` | `CrmContactCrud` by ids, scoped |
| `app/services/import_worker.py:544` | `foreign.contact_by_email` | CRM lookup, scoped |
| `app/services/import_worker.py:547` | `foreign.contact_by_id` | CRM lookup, scoped |
| `app/services/auto_link_service.py` | links a platform contact id | links a `crm_contacts` id |

Because phase 2 preserved ids, the company Contacts tab returns the same people
with the same ids as before. The serialized shape must match what
`sz.contact_ref` already emits, so the UI needs no change in this phase.

### The import-worker rule does not change

`import_worker` reads `contact_by_email` as *"does this person exist?"* and
creates one when told no. The lookup must therefore still **raise** rather than
return `None` when it cannot answer. The CRM lookup is a local query so
"unavailable" now means a database error, which already propagates — but the
test that proves a failed lookup does not create a duplicate should move across
with the call, not be left behind pointed at the old path.

### Suggested safety valve

Gate it on `[CRM] contacts_source = crm | platform`, defaulting to `platform`,
for one deploy. Then stage runs `crm`, you compare a few company pages against
the old behaviour, and phase 4 deletes the flag along with the old branch. If
you'd rather not carry a flag, the alternative is a straight cutover with phase
4 immediately after — acceptable, since phase 2 is non-destructive and the
rollback is flipping three call sites back.

### Acceptance

A company's Contacts tab shows the same people, same ids, same order as before.
A CSV import attaches to existing people instead of duplicating them. The 891
integration tests still pass — most exercise handlers directly and shouldn't
notice.

---

### What phase 3 actually did — APPLIED, AND INERT

Gated on a new `[CRM] contacts_source`, **defaulting to `platform`**. So this
phase can ship and deploy before the backfill has run; flipping the switch is a
separate, deliberate act.

```ini
[CRM]
; contacts_source = platform     ; or `crm`
```
`CRM_CONTACTS_SOURCE` overrides it. Anything other than the exact word `crm`
means `platform` — a typo ("crm_contacts", "true", "CRM_") must land on the
behaviour that works today, not the one that needs a migration to have run.
Verified for each of those spellings.

| File | Change |
|---|---|
| `app/config.py` | `contacts_source`, default `platform` |
| `app/routes/companies.py` | `_fetch_contacts` branches; both callers now pass `account_id` |
| `app/services/import_worker.py` | `contact_email` / `contact_id` note targets branch; cached `CrmContactCrud` |
| `app/routes/internal.py` | `POST /internal/crm/auto-link` refuses 503 while the CRM owns contacts |
| `config/app.conf.example` | documents the flag and when NOT to flip it |

**`auto_link_service.py` needed no change at all.** It writes whatever contact
id it is handed; only the *source* of that id changes. What did need handling is
the platform's way in: `/internal/crm/auto-link` receives PLATFORM ids, and
while `contacts_source = crm` an id for a contact created AFTER the backfill
points at nothing — the link exists and is invisible, with no error. Ids the
backfill did copy still resolve, because it preserves them, so the damage would
be silent and partial, which is worse than total. It now refuses with 503.
Safe to refuse: the platform's `crm_client` logs and returns None on any
failure, so a contact write cannot break — but the platform's log gets a line
saying the hook is still wired after the cutover.

**The CRM branch is account-scoped where the platform branch is not.**
`foreign.contacts_by_ids` is an EXPANSION and `app/foreign.py` explains why it
is deliberately unscoped. The CRM branch scopes anyway, because every other
`crm_*` read in this service is scoped by construction and an unscoped
exception here is what `contact_scope.py`'s defect list is made of. Verified
that another tenant's row is not returned.

#### Verified — 13 checks

```
default source = platform -> today's behaviour, byte for byte
source = crm              -> same id, CRM collection
CRM branch is account-scoped (other tenant's row withheld)
empty id list short-circuits without a query
sz.contact_ref reads a Beanie model and a dict indifferently
internal auto-link: platform -> 200, crm -> 503
typo spellings all fall back to platform
/health unchanged: 12 modules, 16 mounts, 83/83
```

#### ⚠️ THE RISK THAT MATTERS WHEN YOU FLIP IT

`CrmCrudBase.list_by` catches a failed query and returns `[]`. That is
pre-existing and every CRM read relies on it — but it means a broken CRM
branch produces an **empty Contacts tab, not an error**. The harness found this
the hard way: Beanie was uninitialised, the query raised, and the test saw a
clean empty list rather than a failure.

So after flipping `contacts_source = crm`, do not take a quiet deploy as
success. Open a company you KNOW has linked people and confirm they are there.
`--reconcile` reporting zero unresolved is the pre-condition; a populated tab is
the proof.

---

## Phase 4 — remove the platform-contact flow

**The one-way door.** Do this only once phase 3 has run on stage long enough to
trust.

### CRM_API

| File | Change |
|---|---|
| `app/foreign.py` | delete `contact_by_email`, `contact_by_id`, `contacts_by_ids`; **five delegations become two** (users, tags) |
| `app/host/directory.py` | drop the three contact methods from the `Directory` protocol and from `SharedDbDirectory`, `HttpDirectory`, `NoDirectory` |
| `app/services/contact_scope.py` | **delete** — now dead |
| `app/routes/internal.py` | retire `POST /crm/auto-link` (see below) |
| `docs/host-contract.md` | the host surface shrinks; a consumer no longer supplies contacts |
| `config/app.conf.example` | `[HOST] directory` loses its main reason to exist |

Update `docs/host-contract.md` in the same commit. It is the spec a consumer
implements, and a stale one is worse than none.

### emailMarketingAPI

`_auto_link_company` is called at `contacts.py:153` (create) and `:775`
(update), and calls `crm_client.link_one`. With the CRM owning its contacts,
creating a *marketing* contact should no longer reach into the CRM.

**Recommended: remove the hook and the client.** Delete `_auto_link_company`,
its two call sites, `api/services/crm_client.py`, and
`api/routes/internal_crm_host.py`. Clean separation, which is what you asked
for.

⚠️ When you touch `contacts.py:153`, note the comment at line 90: a previous
edit inserted `_auto_link_company` **between** `@router.post("")` and
`create_contact`, so the decorator bound to the wrong function and
`POST /api/v1/contacts` resolved to the helper. That bug is fixed; removing the
helper is another chance to make it. Verify afterwards that
`POST /api/v1/contacts` still resolves to `create_contact` — read the OpenAPI
paths, don't eyeball the file.

**The alternative**, if you want marketing contacts to keep appearing in the
CRM: keep the hook but have it create a `crm_contacts` row with
`platform_contact_id` set and `source = "platform_sync"`. That is a one-way sync
and a standing obligation — every field you want kept current needs updating in
two places forever. I'd remove it and let the CRM own its own people, which is
the point of the change.

### What replaces auto-link

Domain-based company linking is still useful; it just operates on CRM contacts
now. `AutoLinker` stays, driven by `POST /crm/contacts` (phase 5) instead of by
the platform. The `/internal/crm/auto-link` endpoint goes.

That also closes an outstanding security item: it is the one endpoint that acts
on an `account_id` with no end-user token, and
`api-stage-crm.nevtan.com/api/v1/internal/crm/auto-link` currently has only a
shared secret in front of it.

### Acceptance

`grep -rn "contact_by_email\|contacts_by_ids\|contact_scope" app/` returns
nothing outside the deleted files. `/health` still reports `83/83`. The
platform's own contact create/update work with no CRM running at all — which is
the real proof the coupling is gone.

---

### Phase 4a — APPLIED. 4b still pending.

Split deliberately: 4a removes everything that does **not** touch the read
path, so `contacts_source = platform` still works and the rollback is still a
config flip. 4b deletes the platform branch, and that is the one-way door.

#### CRM_API

| Change | Note |
|---|---|
| `app/routes/internal.py` **deleted** | `POST /api/v1/internal/crm/auto-link` was its only endpoint |
| `app/main.py` | the internal router is no longer mounted |
| `app/services/auto_link_service.py` | now has **no caller** — documented, phase 5 wires it from `POST /crm/contacts` |

`git rm` refused `internal.py` because it carried phase 3's 503 gate as an
uncommitted change — the same refusal that broke `step-c-remove-crm.sh` earlier
in this project. `-f` was correct here; the lesson is that it failed loudly.

Deleting this endpoint also closes an outstanding security item: it was the one
endpoint that acted on an `account_id` with no end-user token, behind only a
shared secret, reachable at `api-stage-crm.nevtan.com/api/v1/internal/crm/auto-link`.

#### emailMarketingAPI

| Change | Note |
|---|---|
| `api/routes/contacts.py` | `_auto_link_company` and both call sites (create + update) removed, with its import |
| `api/routes/lists.py` | the batch `crm_client.link_many` block removed from the list importer |
| `api/services/crm_client.py` **deleted** | no callers left |

**`POST /api/v1/contacts` verified from the router, not by reading the file.**
The note above `create_contact` documents a bug where INSERTING a helper
between the decorator and its function silently rebound the route. Removing a
function from that position is the mirror image of the same hazard. Checked:
`POST '' -> create_contact`, 0 route-level deps. The note has been rewritten to
say the helper is gone and that verification goes through OpenAPI.

#### Two things I had wrong in the 4a scope, found by mapping before cutting

**`contact_scope.py` is NOT dead yet.** It is used by
`host/directory.py:151,164` and `import_worker.py`, both of which are the
PLATFORM branch that 4a keeps. Deleting it would have broken the rollback 4a
exists to preserve. → 4b.

**`internal_crm_host.py` serves more than contacts.** Its `KINDS` allow-list is
`{"users", "tags", "contacts"}`, and `HttpDirectory` still needs users and tags
for owner-name and tag expansion after 4b. So the file stays; only the
`contacts` kind and the `_contacts` handler come out, in 4b.

**And a third call site I had not mapped at all:** `api/routes/lists.py:400`
also called `crm_client.link_many`, so deleting `crm_client.py` on the strength
of `contacts.py` alone would have broken the list importer. Found by grepping
for the module rather than for the function.

#### Verified — 14 checks

```
platform branch still works (the rollback 4a must preserve)
  source=platform -> platform row · source=crm -> CRM row
  CRM branch account-scoped · empty list short-circuits
  contact_ref reads both shapes
seam closed, asserted as the OPPOSITE of phase 3's test:
  auto-link endpoint -> 404 under BOTH sources
  app/routes/internal.py no longer on disk
CRM  /health 12 modules · 16 mounts · 83/83 · no /internal/ path
platform  contacts router 10 routes · lists router 18 routes
          POST /contacts -> create_contact
invariants PASS · contract PASS
```

Phase 3's two auto-link assertions failed after this change, correctly — they
tested a gate on an endpoint 4a deletes. Rewritten to assert 404 under both
sources, following `aee58ca`'s `SeamClosedTests` pattern: *"A removed feature
with no test comes back by accident."*

#### Deploy order does not matter

If the CRM ships first, the platform's `crm_client.link_one` gets a 404, logs
it and returns `None` — the failure semantics it always had. If the platform
ships first, the CRM's endpoint is simply unused. Neither order breaks a
contact write.

### Phase 4b — the one-way door, PENDING

Blocked on: the backfill run, and `--reconcile` reporting zero unresolved.

| File | Change |
|---|---|
| `app/foreign.py` | delete the three contact reads; five delegations become two |
| `app/host/directory.py` | drop the three contact methods from the protocol and all three adapters |
| `app/services/contact_scope.py` | **delete** — dead only once the platform branch is gone |
| `app/config.py` | remove `contacts_source` and its platform branch |
| `app/routes/companies.py`, `app/services/import_worker.py` | collapse the branches to the CRM path |
| `emailMarketingAPI/api/routes/internal_crm_host.py` | drop the `contacts` kind from `KINDS` and `_contacts` |
| `docs/host-contract.md` | the host surface shrinks — same commit, a stale spec is worse than none |

---

## Phase 5 — the feature: add a contact directly to a company

### New files

`app/routes/contacts.py` (7 endpoints), plus whatever `contact_rules.py` still
needs.

### Modified

| File | Change |
|---|---|
| `app/profiles.py` | `"contacts"` in `ALL_MODULES`; in `core` if decided |
| `app/main.py` | `_MOUNT_TABLE` entry, import, gated `include_router` |
| `app/main.py:38-39` | `EXPECTED_ROUTERS = 13`, `EXPECTED_ENDPOINTS = 91` |
| `app/auth/rbac.py` | `contacts:*` already exists — check role coverage |

The mount must be a **literal** call inside the profile gate;
`scripts/check_crm_contract.py` parses these via AST and a loop broke it once:

```python
if "contacts" in modules:
    app.include_router(contacts.router)
```

### Endpoints

| Method | Path | Permission |
|---|---|---|
| `GET` | `/crm/contacts` | `contacts:read` |
| `POST` | `/crm/contacts` | `contacts:write` |
| `GET` | `/crm/contacts/{id}` | `contacts:read` |
| `PATCH` | `/crm/contacts/{id}` | `contacts:write` |
| `DELETE` | `/crm/contacts/{id}` | `contacts:write` |
| `POST` | `/crm/contacts/{id}/links` | `contacts:write` |
| `DELETE` | `/crm/contacts/{id}/links` | `contacts:write` |

`GET /crm/companies/{id}/contacts` already exists and keeps its path — it now
serves CRM contacts. No count change there.

Envelopes are fixed: single `{status, message, data}`, list
`{data, total, status}`, business failure at HTTP **200** with
`status: "failure"`.

### The duplicate check

Now a single scoped query — no cross-service call, no `DirectoryUnavailable`
branch, no global index to collide with. Reuse `app/services/dedupe.py`
(`normalize_domain`, `similarity`, `match_confidence`, `find_duplicates`)
rather than writing a second matcher.

```
POST /crm/contacts  { first_name, email?, ... }

email absent  → create
email present → scoped lookup in crm_contacts
                ├─ match    → 200 {status:"failure", data:{duplicate:{...}}}
                └─ no match → create, then AutoLinker for the domain
```

### Acceptance

`/health` → `routers: 13/13, mounts: 17, endpoints: 90/90`.
`check_crm_contract.py` passes. Create a contact via curl, see it on the
company, confirm it is **absent** from the platform's contacts — the point of
the whole design.

---

### Phase 5 — APPLIED. `/health` reports 13/17/91.

**It was not just a mount.** `e1d22e9` stripped contact support out of six
shared rules modules as well as deleting the router, so mounting alone would
have given you endpoints that imported cleanly and then failed at runtime:
no filterable fields, no column catalogue, no bulk actions, no system
attributes, and custom values under typed-column names routed into
`custom_fields` instead of onto the column.

Restored from `e1d22e9^`, ~67 lines across six files:

| Module | Restored |
|---|---|
| `attribute_routing.py` | the `contact` typed-column set — without it a custom `email` lands in `custom_fields` |
| `filter_rules.py` | 20 filterable fields incl. the derived `has_open_task` / `has_overdue_task` |
| `column_rules.py` | `OBJECT_TYPES`, locked `full_name`, the derived-column catalogue, `DEFAULT_COLUMNS` |
| `bulk_rules.py` | `CONTACT_ACTIONS` and the filter allow-list |
| `defaults.py` | the 9 `SYSTEM_CONTACT_ATTRIBUTES` |
| `saved_view_rules.py` | `OBJECT_TYPES` |

`permissions.py` needed nothing — it is object-type agnostic — and
`rbac.PERMISSIONS` already carries `contacts:read` / `contacts:write`, granted
by role, so `check_invariants.py` still passes unchanged.

Two comments came back with their reasoning intact and are worth knowing:
locked columns are per-object because *"a contact's is NOT `name`"*, and no
system contact attribute is `is_required`, because *"a phone-only lead from a
trade show is a real contact"* and the real rule — a name OR an address — is
enforced by `contact_rules.has_identity`, which no single required field can
express.

#### The mount

| File | Change |
|---|---|
| `app/main.py` | `_MOUNT_TABLE` entry, the import, a **literal** gated `include_router`, `EXPECTED_ROUTERS = 13`, `EXPECTED_ENDPOINTS = 91` |
| `app/profiles.py` | `contacts` in `ALL_MODULES` and in `core` |
| `docs/api-contract.md` | regenerated, and the removal note rewritten |

**`contacts` is in `core`** — open decision 3, taken: a CRM a consumer cannot
add a person to is not a core CRM. `core` is now 8 modules / 69 endpoints,
`minimal` stays at 5 / 47 without contacts.

#### The contract took three goes, and each failure was informative

1. `--emit` **supplies its own markers**; wrapping its output in another pair
   produced two of each.
2. The document carried **two opening and four closing markers** already — a
   previous regeneration had inserted rather than replaced. Fixed by replacing
   from the FIRST opening to the LAST closing marker.
3. Even with 91 rows in the table the checker still read 83 documented,
   because the strike-through notes from the removal were **subtracting** the
   contacts endpoints — exactly the mechanism `aee58ca` added so a contract
   could say "this is gone" without re-promising it. The notes had to be
   removed for the restoration to count.

That section has now reversed twice, so it says so, and the two `/link`
endpoints stay struck through — they are the seam that produced two
populations of one person, and they are not coming back.

**`docs/api-contract.md` now states CRM-406 the other way**: an association's
contact side is a `crm_contacts` id, made true for existing rows by the
id-preserving backfill, with `GET /crm/companies/{id}/contacts` resolving
against whichever collection `contacts_source` names.

#### Verified

```
/health      13 modules · 17 mounts · 91/91
profiles     full 13/17/91 contacts=YES
             core  8/10/69 contacts=YES
             minimal 5/6/47 contacts=no
contract     PASS — 91 documented, 91 real
invariants   PASS
phase 3+4a harness  all checks passed (platform branch intact, seam closed)
live router  GET/POST /crm/contacts -> 503 auth-not-configured,
             not a 500 — the restored handlers resolve
```

The 503s are the harness having no JWT key configured, which is the correct
refusal. They prove the handlers and their dependencies resolve; they do not
prove the queries work, which needs a database.

#### Not done here

Contact **import** is still absent — `e1d22e9` removed 167 lines from
`import_worker.py` for it, and it is a feature of its own rather than part of
mounting the router. The import wizard will not offer `contact` as an entity
type.

---

## Phase 6 — UI

| File | Change |
|---|---|
| `src/screens/CRM/Companies/CompanyDetails.jsx` | **+ Add contact** on the existing Contacts section |
| `src/screens/CRM/Contacts/AddContactDialog.jsx` | new |
| `src/screens/CRM/Contacts/ContactDetails.jsx` | new |
| `src/screens/CRM/Contacts/index.jsx` | new — list screen |
| routing + sidebar | new route and nav entry |

No `axiosInstances.js` change: `/crm/contacts` already matches the `/crm/`
prefix rule that routes to the CRM base URL.

Reuse `FilterBuilder`, the saved-views machinery, `ColumnCell`/`columnHelpers`
and `AttributeRenderer`. Mirror `CompanyDetails.jsx`'s layout so the detail
pages stay one app.

On a `duplicate` failure envelope, show the choice — link the existing person,
or create separately — defaulting to link.

`npx eslint` on changed files before finishing; the repo currently has one
pre-existing warning in `Admin/Dashboard/index.jsx` and should have no new ones.

---

### Phase 6 — APPLIED. Restored, not rebuilt.

The UI had its own removal commit, `840efa9` ("CRM UI: remove the Contacts
screen"), so phase 6 was a restore from `840efa9^` rather than the five new
files the plan budgeted for. 1,789 lines back.

| File | Lines | How |
|---|---|---|
| `src/screens/CRM/Contacts/index.jsx` | 528 | checkout |
| `src/screens/CRM/Contacts/ContactDetails.jsx` | 454 | checkout |
| `src/screens/CRM/Contacts/ContactFormDialog.jsx` | 212 | checkout |
| `src/screens/CRM/shared/contactHelpers.js` | 185 | checkout |
| `src/screens/CRM/shared/contactHelpers.test.mjs` | 252 | checkout |

Ten more files had their contact support stripped rather than deleted. Five had
not been touched since the removal, so a straight `checkout 840efa9^` was
exact: `features.js`, `routeMap.js`, `screens/index.js`, `BulkActionBar.jsx`,
`attributeFormHelpers.js`. Four HAD changed since — `Utility/crm.js`,
`Environment.jsx`, `menuConfig.js`, `ColumnCell.jsx` — so the removal's own
diff was **reverse-applied** per file (`git show 840efa9 -- <file> | git apply
-R --3way`) instead, which keeps the later work. All four applied with no
conflict.

That mattered for one file in particular: `Environment.jsx` is where
`crmApiBaseUrl` was added earlier in this project, four commits after the
removal. A straight checkout would have silently deleted the setting that
routes `/crm/` calls to the CRM service. Verified afterwards that both the
restored `CRM_CONTACTS*` endpoints and `crmApiBaseUrl` are present.

#### The one genuinely new piece

`CompanyDetails.jsx` never had an add-contact button — the original design only
let you **attach someone who already existed**, through `EntityPicker`. That is
exactly the gap this whole piece of work started from, so a **"+ New contact"**
button now sits beside the picker.

It creates **then** links, in two calls, rather than adding an endpoint:
`ContactFormDialog` makes the person and the existing `attachContact` links
them. The form has no company field — `attributeFormHelpers` omits one on
purpose, because a contact's company is a `crm_associations` row and not a
column a form can set — so the link has to be a second step either way.

⚠️ **If the link fails the contact still exists.** `attachContact` reports its
own failure and the person is in the CRM regardless, so the outcome is "created
but not linked on this company", fixable from the Contacts screen. Deleting the
contact to make the pair atomic would be the worse trade: it throws away what
the user typed.

#### Verified

```
node --test   414 tests, 414 passed   (was 378; +36 from contactHelpers.test.mjs)
eslint        0 problems on every file touched
sidebar       crm-contacts entry restored, permission contacts:read
routes        /crm/contacts and /crm/contacts/:contactId, lazy-loaded
Environment   CRM_CONTACTS* endpoints AND crmApiBaseUrl both present
```

`scripts/crm-tests.mjs` `EXPECTED_TESTS` updated 378 → 414 in the same change,
which is the rule that file states: fewer than expected is a failure, more only
warns and asks for the constant to move.

#### ⚠️ The suite still reports FAIL, and it is not this work

12 eslint errors, all in `src/screens/CRM/Companies/index.jsx` and
`src/screens/CRM/Deals/index.jsx` — unused `FilterBuilder`, `SavedViewBar`,
`FileUploadOutlinedIcon`, `adoptView`, `applyFilter`. Both files are unmodified
by this phase, and linting `HEAD`'s own copy of `Deals/index.jsx` reports 6 of
them, so they predate it. The shape — imported-but-unused filter and saved-view
components — reads like a refactor somebody is in the middle of, so they are
left alone rather than "fixed" by deleting imports whose use may be about to
come back.

---

## Phase 7 — leads

Tiny, because of the phase-0 decision.

| File | Change |
|---|---|
| `app/enums.py` | `AssociationEntity.LEAD` only if leads are a separate *entity* — under the merged model they are **not**, so no enum change at all |
| `app/services/associations.py` | likewise unchanged |
| `app/models/contact.py` | `lifecycle_stage` already present from phase 1 |
| `app/services/contact_rules.py` | allowed stage transitions |
| UI | a stage filter and a badge; "Qualify" is a `PATCH` |

If you choose two collections instead (decision #1), phase 7 becomes the whole
of the first draft's phases 1–5 and both phase-0 ordering guards start earning
their keep immediately.

Under the merged model, `lifecycle_stage` is what keeps leads out of marketing:
they are in `crm_contacts`, and campaigns read `contacts`. Nothing to enforce.

### Acceptance

A contact created from a company starts at `lead`. Qualifying is one `PATCH`.
No lead appears in any segment or campaign audience.

---

### Phase 7 — APPLIED. Mostly already there, and it exposed a live bug.

The merged-collection decision made this the smallest phase, as planned:
`lifecycle_stage` is a field on `CrmContactModel`, `contact_rules.LIFECYCLE`
defines the vocabulary, and the restores in phases 5 and 6 brought back the
filter, the sortable column, the system attribute and the detail-page badge.
Qualifying a lead is a `PATCH` on one field. No model change, no migration, no
enum change — `AssociationEntity` and `_PRECEDENCE` are untouched, so phase 0's
two ordering guards never had to fire.

#### ⚠️ What verifying it found: the two lists had NEVER agreed

```
UI      lead · marketing_qualified · sales_qualified · opportunity ·
        customer · evangelist · other          (7)
server  subscriber · lead · qualified · customer · churned   (5)
```

Overlap: `lead` and `customer`. Checked `e1d22e9^` and `aee58ca^` — the server
has carried its five since before either removal, so these were never in sync,
and the comment above the UI list claimed it mirrored
`contact_rules.LIFECYCLE`.

What that cost, confirmed against `clean_lifecycle`:

* five of seven options came back **"is not a lifecycle stage"** from the form;
* used as the quick filter, those five asked the server for rows no record can
  have and rendered an **empty list with no error**;
* `subscriber` and `churned`, which the server accepts, were **unreachable from
  the UI at all** — including `subscriber`, which is the stage
  `backfill_crm_contacts.py` stamps on every row it writes. The entire
  back-filled population would have been invisible to the one filter built for
  it.

Across two repositories rather than two files, which is why neither side's
tests caught it.

#### The fix: one definition, pinned twice

| Change | Where |
|---|---|
| `LIFECYCLE_STAGES` and `stageLabel` exported once | `shared/contactHelpers.js` — the module whose stated job is "one rule in one place" |
| local copies deleted | `Contacts/index.jsx` (list + label), `ContactDetails.jsx` (label — a third copy of the same two lines) |
| 4 tests pinning the list, its order and the label | `contactHelpers.test.mjs` |
| a **cross-repo** invariant | `scripts/check_invariants.py`, fourth check |

The invariant parses the JS array as a list of string literals rather than
grepping for a name — a `grep` for `subscriber` would pass on the file's own
comment, which now quotes both the right list and the wrong one. It is checked
only when `../emailMarketingUI` is a sibling checkout (the same assumption
`check_crm_contract.py` already makes) and is reported as **skipped, never as a
pass**, when it is not.

Order is pinned as well as membership, because it is the dropdown's order and
the funnel's: subscriber → lead → qualified → customer, churned last. Sorting
it alphabetically would put `churned` second.

#### Verified

```
node --test        418 tests, 418 passed (was 414; +4)
eslint             clean on every file touched
invariants         PASS — "...the UI's lifecycle stages match the server's"
MUTATION  old 7-value list restored  -> test 37 FAILS
MUTATION  wrong members              -> invariant FAILS, names both lists
MUTATION  right members, reordered   -> invariant FAILS
server     the five accepted, marketing_qualified / opportunity refused
```

`EXPECTED_TESTS` 414 → 418 in the same change.

#### Still FAIL on the suite, still not this work

The same 12 pre-existing eslint errors in `Companies/index.jsx` and
`Deals/index.jsx`. Untouched by phases 6 and 7.

---

## Phase 8 — attributes, notes, tasks

Additive; not a blocker for phase 6.

**Custom attributes.** `CrmObjectType.CONTACT` already exists, so contacts get
the attribute system with no enum change. `attribute_routing` routes values
generically and `AttributeRenderer` handles all 13 types.

**Notes and tasks.** `CrmNoteModel.contact_ids` and `CrmTaskModel.contact_ids`
already exist and, after phase 2's id preservation, already point at the right
rows. This phase is mostly verification rather than new fields — the main reason
the merged model is simpler than two collections, where a parallel `lead_ids`
array would have been needed on both models.

### Acceptance

A custom attribute on `contact` renders and saves. Notes and tasks written
before the cutover still resolve to the right person.

---

### Phase 8 — APPLIED. Two more gaps the restores had left behind.

Mostly verification, as expected — and the verification was the point.

#### Already correct, confirmed rather than assumed

| Thing | Why it already worked |
|---|---|
| `CrmObjectType.CONTACT` | never removed from the enum |
| `app/schemas/attribute.py` | types `object_type` as `CrmObjectType`, so **create and update always accepted contact** |
| `notes.py PARENTS` | `"contact": ("contacts:read", "contacts:write")` survived |
| `tasks.py` | handles `contact_ids`, emits `entity_type: "contact"` |
| `imports.py NOTE_PARENT_PERMISSIONS` | already carried `contact` |
| `activity_recorder.ENTITY_LABELS` | already carried `Contact` |
| `scripts/seed_crm_defaults.py` | iterates `defaults.SYSTEM_OBJECT_TYPES`, derived from the dict phase 5 fixed — its own comment says a type appears "the moment it joins SYSTEM_ATTRIBUTES_BY_OBJECT" |
| `contact_ids` on notes and tasks | the id-preserving backfill means they already point at the right rows |

#### ⚠️ Two gaps found in `app/routes/attributes.py`

**1. `GET /crm/attributes?object_type=` refused `contact` outright.**

```python
None, pattern="^(deal|company)$",
```

A pattern mismatch is a FastAPI **422 before the handler runs**. The restored
`ContactFormDialog` fetches
`GET /crm/attributes?object_type=contact&show_on_form=true` — so it got
"Unprocessable Entity", fell through to `attributeFormHelpers`'s hard-coded
set, and **still rendered**: nine system fields, no custom contact attribute
ever, and nothing in the UI to say so. That fallback's own comment calls itself
load-bearing, which is exactly why this would have gone unnoticed.

Note the asymmetry it created: the WRITE side is typed `CrmObjectType`, so you
could create a contact attribute and then never read it back through the
filtered endpoint.

**2. The delete-confirmation dispatch had no `contact` arm** — and the comment
directly above it describes the contact case as the reason the lookup exists:

> *"⚠️ A LOOKUP, NOT `companies if ... == "company" else deals`. That spelling
> counted a CONTACT attribute's values against the DEALS collection..."*

With no arm, every contact attribute hit `"That attribute is on an object this
endpoint cannot check."` — so a contact attribute **could not be deleted at
all**, and the message blamed the endpoint rather than naming the gap. Another
instance of a comment describing behaviour the code no longer delivers; the
same shape as the `sparse` index in phase 1 and the lifecycle list in phase 7.

Fixed, with `CrmContactCrud` injected into the handler.

#### Verified — 24 checks against an in-memory Mongo

```
system catalogue   contact is a SYSTEM_OBJECT_TYPE, builds 9, none required
routing            email is a typed column; full_name is NOT (derived);
                   a custom name goes to custom_fields
round trip         custom_fields and lifecycle_stage survive insert/read
notes & tasks      found by contact id; the id IS the contact's
tenancy            another account sees neither the contact nor its notes
dispatch           notes PARENTS gates on contacts:*;
                   attribute delete dispatches contact;
                   the query validator allows contact
layouts            saved_view_rules and column_rules accept contact;
                   locked column is full_name, not name
MUTATION  revert both attribute fixes -> both checks FAIL
```

Plus, unchanged by this phase: contract PASS at 91/13, invariants PASS, the
phase 3/4a harness green, `/health` 13 modules / 17 mounts / 91/91.

#### One thing to decide before this reaches data

`scripts/migrate_crm_contacts_removal.py` is still in the repo, and it is now
the **inverse** of this work: it repoints `crm_notes.contact_ids` and
`crm_tasks.contact_ids` from CRM contact ids to PLATFORM ones, matching by
email within the account. It is report-only unless `--apply` is passed and it
deletes nothing, so if it was never applied nothing has moved. If it WAS
applied, a note may have been repointed to a different person than the CRM
contact it was written against — matching was by email, and the CRM stored
email normalised while `Contact.email` is not lowercased.

Worth answering before the backfill runs: **was that script ever run with
`--apply`?** If yes, the notes and tasks it touched need checking against
`crm_contacts` once the backfill lands.

---

## Open decisions

**1. One collection or two?** Recommended: one `crm_contacts` with
`lifecycle_stage`. Shapes phases 1, 7 and 8, and is the difference between
phase 7 being a field and being five phases.

**2. Keep or remove the platform → CRM auto-link hook?** Recommended: remove
(phase 4). Keeping it means a one-way sync maintained forever.

**3. Is `contacts` in the `core` profile?** I'd say yes — a CRM you cannot add a
person to is not a core CRM — making `core` 8 modules.

**4. Delete semantics.** Soft or hard, matching `companies`. A row with
`platform_contact_id` set is backfilled history and should probably resist hard
deletion.

**5. Carry the phase-3 flag or cut straight over?** A flag is one deploy of
safety and one more branch to delete. Either is defensible; phase 2 being
non-destructive is what makes a straight cutover reasonable.

---

## Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| Backfill loses or duplicates people | 2 | upsert on preserved `_id`; idempotent; reconciliation mode; dry run first |
| `_PRECEDENCE` drifts from the enum | 0 | the agreement test — **already a live latent bug** |
| Association ids orphaned by the cutover | 2, 3 | preserve `_id`; reconcile before phase 4 |
| `@router.post("")` rebinding when the hook is removed | 4 | verify from OpenAPI paths, not by reading the file |
| Contract constants missed | 5 | `/health` counts + `check_crm_contract.py` |
| A second copy of a shared rule | all | import from `dedupe.py`, `associations.py`, `attribute_routing` — never retype |
| Guard that matches its own docstring | all | assert on AST or behaviour, never a source grep |
| `host-contract.md` left stale after the host surface shrinks | 4 | same commit |
| Phase 4 done too early | 4 | it is the only irreversible phase; gate it on stage evidence |

---

## No backfill: the `crm_contact` association type

The migration was declined. Rather than move ids into `crm_contacts`, the
association row now says which collection its contact id lives in.

```
crm_associations
  company --to_type="contact"------>  contacts       (existing, platform)
  company --to_type="crm_contact"-->  crm_contacts   (new, CRM-owned)
```

**Nothing is rewritten and nothing is lost.** Every row written before 23 Oct
2026 keeps its meaning and keeps resolving against the marketing audience;
every CRM contact created from now on links as `crm_contact`. Each id is looked
up in exactly one collection, decided by the row, not by a flag or a fallback.

This also *answers* CRM-406 rather than deferring it. The ambiguity `aee58ca`
named — *"a contact row could hold either a platform id or a CRM one depending
on which screen wrote it"* — is removed by making the two cases different rows.
The alternative on the table, looking each id up in one collection and falling
back to the other, would have made that ambiguity permanent.

### What changed

| File | Change |
|---|---|
| `app/enums.py` | `AssociationEntity.CRM_CONTACT` **appended** |
| `app/services/associations.py` | `_PRECEDENCE` gains `"crm_contact": 3` |
| `scripts/check_invariants.py` | `EXPECTED_ASSOCIATION_ORDER` updated |
| `app/schemas/company.py` | `CompanyLinksRequest.crm_contact_ids`, `CompanyDetails.linked_crm_contacts` |
| `app/routes/companies.py` | `_fetch_crm_contacts`; `_fetch_contacts` is platform-only again; the detail endpoint fetches both |
| `app/services/serializers.py` | `linked_crm_contacts` passed through, on `is not None` |
| `app/config.py`, `config/app.conf.example` | `contacts_source` narrowed to the note importer only |
| `docs/api-contract.md` | CRM-406 restated as a table of the two `to_type`s |
| `scripts/backfill_crm_contacts.py` | **deleted** (was untracked, so `rm`, not `git rm`) |
| `emailMarketingUI/.../CompanyDetails.jsx` | a "CRM contacts" section; "+ New contact" posts `crm_contact_ids` |

### Phase 0's guards paid for themselves here

Appending to `AssociationEntity` without touching `_PRECEDENCE` is exactly the
latent bug phase 0 was written for, and it fired on the first run:

```
✗ association-order:  pinned as [company, deal, contact]
✗ precedence-agrees:  _PRECEDENCE is [company, deal, contact]
                      but AssociationEntity is [..., crm_contact]
```

Without it, `normalize_pair` would have raised `InvalidAssociation` on every
company↔CRM-contact link — confirmed by mutation, which also showed the
invariant catching it.

### Two UI traps worth naming

**The new contact must post `crm_contact_ids`.** Under `contact_ids` the
association would say "platform contact", the tab would look the id up in the
marketing collection, find nothing, and the link would exist and be invisible
with no error. The call site says so.

**The sections are headed, and only when both exist.** A company whose people
are all platform contacts looks exactly as it did; once a CRM contact appears,
both lists get a heading. One population is marketable and the other is not, so
a reader has to be able to tell which person is which — merging them into one
table would sit a person who never consented next to one who did.

### Verified

```
p9 (new)       22 checks — counterparts separates the two by to_type;
               each id read from exactly ONE collection; a platform id is
               NOT found in crm_contacts and vice versa; normalisation;
               the links payload; empty-list serialisation
MUTATION       crm side reads to_type="contact"  -> caught (check 8)
MUTATION       _PRECEDENCE entry dropped         -> InvalidAssociation
                                                   + invariant FAILS
p8             24 checks, unchanged
p3             rewritten: asserts contacts_source NO LONGER affects the tab
invariants     PASS   contract PASS   /health 13/17/91
UI             418 tests passed; eslint clean on every file touched
```

**Check 8 exists because the harness had a gap.** Checks 1–7 call
`counterparts` and `_fetch_*` directly, so none of them exercised the route's
wiring — the first mutation passed when it should have failed. Check 8 asserts
on the AST of `companies.py` that the route asks for both `want_type`s, and a
grep would not have done: `"crm_contact"` appears several times in that
module's own comments.

### What is left

* **Phase 4b** no longer depends on a backfill, but its shape changes: the
  three contact reads in `foreign.py` and `host/directory.py` are now
  **permanent**, because `to_type="contact"` rows are permanent. What 4b can
  still remove is `contact_scope.py`'s use by the note importer, and the
  `contacts` kind from `internal_crm_host.py`, only if `contacts_source` is
  fixed at `crm` — i.e. only if you accept that a `contact_email` column in a
  note import means a CRM contact. Otherwise 4b is **closed with nothing to
  do**, and that is the honest outcome.
* `migrate_crm_contacts_removal.py` is still in the repo and still the inverse
  of this work. With no backfill it matters more, not less: if it was ever run
  with `--apply`, notes and tasks were repointed to platform ids by email
  match. Those rows are now correct for `to_type="contact"` and wrong for any
  note that was about a CRM contact.
