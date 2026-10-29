# Contact unification — phases

Execution plan for `docs/contact-unification-plan.md`. Written so each row can
become a ticket as it stands.

**Shape of the work:** phases 1–3 are additive and change no behaviour. Phase 4
onward is behind a flag, one account at a time. Phase 7 is the only
irreversible step, and it is deliberately placed *after* both paths have run
side by side.

| | Phase | Repo | Size | Blocked by | Reversible |
|---|---|---|---|---|---|
| 0 | Decisions | — | — | — | ✅ settled |
| 1 | Profile model + CRUD | CRM_API | S | — | ✅ done |
| 2 | Person fields on the platform contact | platform | S | — | ✅ done |
| 3 | The cutover flag | CRM_API | S | 0.1 | ✅ done |
| 4 | Read path — default view | CRM_API | **L** | 1,2,3 | ✅ done |
| 4b | Read path — "All contacts" view | CRM_API | M | — | ✅ done¹ |
| 5 | Write path | CRM_API + UI | **L** | 4 | ✅ done¹ |
| 6 | One id space | CRM_API | M | 5 | ✅ done |
| 7 | Migration | CRM_API | **L** | 6 | written ⚠️ not yet run |
| 8 | UI | UI + CRM_API | M | 4,5 | ✅ done¹ |
| 9 | Retire `crm_contacts` | both | S | **7 must RUN** | **no** — gated |

---

## Phase 0 — decisions ✅

**Settled 29 Oct 2026.** Recorded in `contact-unification-plan.md` §6.

| | Decision | Consequence |
|---|---|---|
| 0.1 | Per-account flag **and** an env kill switch | phase 3 builds both; the helper reads env first, then the account |
| 0.2 | Worked contacts by default, explicit "All contacts" view | phase 4 drives the aggregation from **`crm_contact_profiles`**, so owner and lifecycle stay index-served; the All view drives from `contacts` and accepts the slower sort |
| 0.3 | `promote` **removed in phase 5** | pulls a UI change into phase 5 — see below |
| 0.4 | CRM delete archives the profile only | phase 5 never writes subscription state; phase 8 shows two distinct actions |

⚠️ **0.3 has a caller nobody expected.** `promoteContact` is used by
`src/screens/CRM/Deals/DealDetails.jsx:855`, not just the Contacts screen. The
endpoint cannot be deleted ahead of that call site, so phase 5 ships with a
small UI change and takes `EXPECTED_ENDPOINTS` 97 → 96 plus a regenerated
`docs/api-contract.md`. That bookkeeping was pencilled into phase 9 and has
moved.

## Phase 1 — profile model + CRUD ✅

**Shipped 29 Oct.**

- `app/models/contact_profile.py` — `CrmContactProfileModel`, registered in
  `app/db.py`. Unique `(created_by, contact_id)` plus four supporting indexes.
  Additive, so unlike `CrmContactModel` it cannot conflict on deploy.
- `app/crud/contact_profile.py` — `CrmContactProfileCrud`, exported from
  `app/crud/__init__.py`: `for_contact`, `for_contacts` (one query per page,
  not per row), `ensure` (the lazy-create primitive) and
  `rename_company_fanout`.

Nothing reads either yet.

⚠️ **`ensure` is read → insert → re-read on conflict, not a pre-check.** Two
requests editing the same unworked contact both find nothing and both insert;
`uniq_crm_contact_profile` lets one win, and catching the duplicate key makes
the loser correct rather than a 500. The gap between a pre-check and its insert
is the whole race, so a pre-check alone cannot close it.

⚠️ **No `list()` / `count()` here on purpose.** Those need
`queries.profile_filter` and the two pipelines phase 4 designs — and 0.2
decides which collection drives each. A half-right filter shipped early is a
thing phase 4 would have to unpick.

Verify: `python3 scripts/check_invariants.py && python3 scripts/check_crm_contract.py`

---

## Phase 2 — person fields on the platform contact ✅

`emailMarketingAPI/app/db/models/main/contacts.py`

Add `job_title` and `linkedin_url`, both `Optional[str] = None`. They describe
the **person**, not the relationship, so they belong on the contact and not in
the sidecar — see the split table in the plan.

- Additive and nullable: no migration, no index change, no reader affected.
- Do **not** move `custom_fields`. The two dicts stay separate (plan §2).

**Shipped 29 Oct**, plus a compliance fix it would otherwise have introduced.

⚠️ **`_PII_FIELDS` IS THE WHOLE OF RIGHT-TO-BE-FORGOTTEN.**
`api/services/engage/compliance.py::anonymize_contact` scrubs exactly the keys
in that tuple and copies everything else through untouched. Adding two
identifying columns to `Contact` without listing them there produces a record
that *looks* anonymised — name gone, email tombstoned — while still carrying
the person's employer and a link to their profile. Confirmed against the
pre-fix tuple: `{'job_title': 'VP Sales', 'linkedin_url': 'https://…/ann'}`
survived erasure. Both are now listed, with a note on the tuple saying that a
new person field on `Contact` means a new entry here.

`build_dsar_export` needed no change — it copies the whole profile, so new
fields are exported automatically, which is correct for a subject access
request.

Pinned by `tests/test_engage_compliance.py::test_anonymize_scrubs_crm_person_fields`.
31 tests pass (`python3 -m unittest tests.test_engage_compliance`; pytest is
not installed in the dev VM, but the suite is unittest-based).

**Rollback:** drop the two fields and their `_PII_FIELDS` entries; nothing
reads them until phase 5.

---

## Phase 3 — the cutover flag ✅

**Shipped 29 Oct.** One switch pair, read in exactly one place.

- `app/config.py` — `settings.unified_contacts: Optional[bool]`, from
  `CRM_UNIFIED_CONTACTS` or `[CRM] unified_contacts`. **Tri-state:** `None`
  hands each account its own flag, `False` is the kill switch, `True` ends the
  rollout.
- `app/models/settings.py` — `CrmSettingsModel.unified_contacts: bool = False`.
- `app/services/contact_scope.py` — `unified(account_settings)` and
  `unified_override()`.
- `app/main.py` — `/health` reports `unified_contacts.override`.
- `app/routes/settings.py` — `GET /crm/settings` returns the account's flag and
  `unified_contacts_effective`.

| env override | account flag | `unified()` |
|---|---|---|
| unset | false | **false** |
| unset | true | **true** |
| unset | settings unloadable | **false** |
| `on` | either | **true** |
| `off` | either — *including true* | **false** |

⚠️ **An unrecognised env value falls through to `None`, not to off** — the
opposite of `task_reminders` beside it, and deliberately. There a typo must not
start a second scheduler, so anything unclear means off. Here *"off" is itself
an override* that would yank every account back onto the old path; a typo must
not do that either. Falling through leaves the deployment in the state it was
already in. Verified against `""`, `true`, `ON`, `0`, `off` and `banana`.

⚠️ **Unloadable settings answer `false`, not an error.** A transient settings
read must never move a user's contacts to a different collection for one
request.

⚠️ **The account flag is READ-ONLY over the API.** It is absent from
`CrmSettingsUpdateRequest` on purpose: an account admin must not be able to
move themselves onto a half-finished read path from the settings screen. Ops
sets it directly during the rollout.

**Correction to this plan as first written:** it said *"/health reports which
mode an account is in"*. It cannot — `/health` has no account context. The
override is reported there; the per-account value and its effective result are
on `GET /crm/settings`.

**The single-read rule.** `unified()` is the only place either switch is
consulted for a decision — confirmed by grep: every other mention is
declaration, parsing or display. Phases 4–6 must ask this function and never
re-read the setting. The failure mode of a flag read in two places is not an
outage; it is one request serving a contact from `crm_contacts` and the next
from `contacts`, for the same account, which is indistinguishable from a
caching bug.

Verify: `python3 scripts/check_invariants.py && python3 scripts/check_crm_contract.py`

## Phase 4 — read path (default view) ✅

**Shipped 29 Oct.** `GET /crm/contacts` and `GET /crm/contacts/{id}` serve the
platform contact joined to its profile when `contact_scope.unified()` says so.
Response shape is unchanged — `payload()` and `unified_payload()` emit the same
24 keys, asserted by a key-set diff.

### What this plan got wrong

⚠️ **THE `$lookup` IN THE ORIGINAL PHASE 4 WAS NOT POSSIBLE.** It said the list
would be "an aggregation: `contacts` scoped by `contact_scope.scoped()`,
`$lookup` into `crm_contact_profiles`". But `contacts` is not reachable as a
collection — it is behind the `Directory` interface in `app/host/directory.py`,
which has **three** implementations: one shared database, one HTTP call to the
host, one none at all. A join exists only in the first. Anything the list sorts
or filters on therefore has to be local to the CRM.

### The consequence: an identity cache

`full_name`, `email` and `job_title` are denormalised onto
`CrmContactProfileModel`, because the list sorts by name and searches name,
email and company — all facts about the PERSON. Precedent is everywhere in this
module: `primary_company_name`, `CrmContactModel.full_name`, `tasks.assigned_to_name`.

- **Exactly those three**, matching `profile_sort` and `profile_filter`'s `q`.
  Every addition is another field to drift.
- **Rendered from the person, not the cache.** `unified_payload` reads name and
  email off the contact, so a stale cache is a missed search hit, never a wrong
  name on screen.
- **Drift is repaired out of band** by `scripts/reconcile_contact_profiles.py`
  (`--dry-run` / `--apply`), which pages profiles and refreshes them through the
  existing `contacts_by_ids` door — no new host read. `refresh_identity` writes
  only when a value actually changed, because `updated_at` is a sort key and a
  blind nightly `$set` would reshuffle everyone's recently-touched view at 3am.

### Also shipped

- `queries.profile_filter` / `profile_sort` — deliberately the same shape as
  `contact_filter` / `contact_sort`, so flipping the flag cannot change which
  contacts an account can find. `linked` is **dropped**, not ignored: every row
  is already a marketing contact, so the question has no answer.
- `CrmContactProfileCrud.list` / `count` — same builder, same arguments.
- `_unified_details` in the route, and `cr.unified_payload`.
- `GET /crm/contacts/{id}` takes a **platform contact id** on this path, and a
  missing profile is **not a 404** — that is a marketing contact nobody has
  worked yet, which is exactly how somebody opens a record in order to start
  working it.
- The association reads follow the path (`contact` vs `crm_contact`) until
  phase 6 converges the writers.

Verify: `check_invariants.py`, `check_crm_contract.py`, and a key-set diff of
the two payload builders.

---

## Phase 4b — the "All contacts" view ✅

**Shipped 29 Oct.** The second half of decision 0.2.

¹ **Complete on the shared-database and no-host adapters. The HTTP adapter's
client half is written and needs a platform endpoint** — see below.

### The sixth host read, taken deliberately

`app/foreign.py` argues that *"the five call sites are the thing that must not
grow"*. This grows them, because the other five can only resolve ids a caller
already holds, and listing the audience means paging a collection this service
does not own. So `contacts_page(db, account_id, q, skip, limit, sort)` was
added to the `Directory` Protocol and to all three adapters, with the
reasoning recorded at the Protocol and at the door.

| adapter | behaviour |
|---|---|
| `SharedDbDirectory` | real query, **account-scoped via `contact_scope.scoped`**, case-insensitive regex on email / first / last name, with a true `count_documents` total |
| `HttpDirectory` | posts `kind: "contacts_page"`; **the host endpoint does not exist yet.** Degrades to `([], 0)` rather than raising, this adapter's rule for a read the host cannot answer |
| `NoDirectory` | `([], 0)` |

⚠️ **AN HTTP-DIRECTORY DEPLOYMENT THEREFORE SEES AN EMPTY ALL VIEW**, not an
error, until the platform implements `contacts_page`. The default view is
untouched because it needs no host paging. That is the remaining work for this
phase, and it lives in the platform repo.

### The view itself

`GET /crm/contacts?scope=all`, unified path only (`worked` is the default and
the parameter is ignored on the legacy path, which has one population).

⚠️ **IT INVERTS WHICH HALF IS INDEX-SERVED, AND SO IT REFUSES THE FILTERS IT
CANNOT HONOUR.** Driving from `contacts` means name and email search are fast
— that is what the view is for — but `owner_id`, `company_id`,
`lifecycle_stage`, `tag_ids` and the advanced `filters` describe a CRM
relationship that exists only on the profile, and there is no join across the
host boundary. Passing one returns a `failure` envelope naming the offending
filters and pointing at the default view. **Silently returning the unfiltered
audience is the failure that reads as "the filter does nothing"** and gets
debugged for an afternoon.

Profiles for the page are fetched in one batched `for_contacts` call — the
mirror of the default view, which batches the people instead. A contact with
no profile renders with empty relationship fields.

`docs/api-contract.md` records both caller-visible differences. The endpoint
count is unchanged, so `check_crm_contract.py` still passes.

## Phase 5 — write path ✅

**Shipped 29 Oct.** ¹ Same caveat as 4b: the HTTP adapter's client half is
written, the platform endpoint is not.

### What this plan got wrong, again

⚠️ **`foreign.py` FORBIDS WRITES** — *"There is no `write_*` function here, and
there should never be one."* Nothing in this service wrote to `contacts`, and
a unified create **is** creating a marketing contact. The plan said "create is
find-or-create" without saying through what.

Resolved by honouring the rule rather than overruling it: reads stay read-only
in `foreign.py`, and writes get their own interface and their own door.

- `app/host/writer.py` — `ContactWriter` Protocol + `SharedDbWriter`,
  `HttpWriter`, `NoWriter`, selected by `host_directory` (one host, one
  transport — a second setting is how a deployment ends up reading over HTTP
  and writing direct).
- `app/host_writes.py` — the named write door. Separate file so `foreign.py`'s
  count of host READS keeps meaning something.

| | read adapter rule | write adapter rule |
|---|---|---|
| host can't answer | degrade to empty | **raise** |

A read that answers empty degrades a screen; a write that answers "fine" and
stores nothing loses data and tells the user it was saved. `NoWriter` refuses
with a message naming the setting to change.

### The consent decision

⚠️ **`Contact.subscribed` DEFAULTS TO `True` ON THE MODEL.** A create that
merely omits it opts a sales lead into marketing — a consent decision made by
forgetting, in a product whose compliance story is that CRM presence is not
marketing permission. `contact_rules.new_person_fields` forces
`subscribed=False` and stamps **no** `subscribe_date`, because that field is
the record of when consent was given and writing it here would forge one.
Asserted in the field-split check.

### The rest

- `cr.PERSON_FIELDS` / `PROFILE_FIELDS` / `split_updates` — one PATCH body
  sorted in two. **A key in neither set is dropped, not guessed at:** routing
  an unknown field to the profile writes it into a schema that has no place
  for it, where it is never read back and never reported.
- **Create's duplicate check moved to the profile.** An address already in the
  audience is the normal case, not a duplicate — refusing it would make the
  unified CRM unable to work anybody marketing found first. Having already
  *worked* them is the duplicate.
- **`find_or_create_contact` lives in the adapter**, because the global unique
  index forces a global read that scoping would break. That is mechanical, not
  policy.
- **Delete archives the profile only** (0.4) and says so in the response: *"…
  They remain in your marketing audience."* `hard` removes the profile, never
  the person — the only thing this account owns is the relationship.
- **Lazy profile creation on edit**, since setting an owner on an unworked
  contact is exactly how somebody starts working them.
- **A `False` from `update_contact` is reported, not swallowed** — the row may
  belong to another account that shares it via `owner_ids`.

### `promote` removed (0.3)

Route, `ContactPromoteRequest` import, and 138 lines gone.
`EXPECTED_ENDPOINTS` 97 → 96; `api-contract.md` records the removal with the
`~~`…`~~` strikethrough the checker requires, or naming it re-adds it.

UI in the same release: the **"Add to CRM" button on the Deal page**,
`promoteContact` in `Utility/crm.js`, the `CRM_CONTACTS_PROMOTE` route
constant, and the now-unused `canWriteContacts`. Left behind, it would have
404ed from the Deal page — not where anyone looks for the fallout of a
contacts change.

Verify: `check_invariants.py`, `check_crm_contract.py` (96), `eslint
src/screens/CRM/` clean, 414 UI tests pass.

## Phase 6 — one id space ✅

**Shipped 29 Oct.** New links name the person, not the CRM row.

`associations.person_entity(unified)` is the **one place** that decision is
made — the same rule as `contact_scope.unified` for the flag itself, and for
the same reason: a writer that disagrees by one branch produces rows nothing
can find. Six call sites, all through it.

| file | sites |
|---|---|
| `routes/deals.py` | create links, add links, remove links |
| `routes/companies.py` | `_mutate_links`, and the detail read |
| `routes/contacts.py` | the detail read (now uses the helper too) |

⚠️ **`AssociationEntity.CRM_CONTACT` STAYS.** Its POSITION defines the
canonical direction a pair is stored in, so deleting the member re-orders the
ones after it and orphans every row already written. Legacy — read, never
written — is a different thing from gone. `check_invariants.py` pins this.

### Two things the plan listed that did not need changing

- ⚠️ **`crm_tasks` and `crm_notes` need no code.** The plan named
  `models/task.py` and `models/note.py`. Their `contact_ids` are **opaque** —
  nothing validates them against either collection, and `routes/tasks.py` says
  so outright (*"Contacts are not ours; the id is returned without a name"*).
  The arrays simply hold whichever id space the writer used. They are a phase
  7 remap, not a phase 6 change.
- ⚠️ **`GET /deals/{id}/contacts` needed no flag.** It already reads BOTH
  types and labels each row's `source`. New `contact` links land in the first
  set, pre-cutover `crm_contact` links in the second, and both render — which
  makes it the one endpoint that is correct *during* the migration as well as
  on either side of it. A half-migrated deal shows all its people, not half.

### The company Contacts tab

`CompanyDetails.jsx` renders `linked_crm_contacts` and nothing else, so on the
unified path the platform rows arrive under that key — otherwise the tab goes
empty on an account whose people are all still there. `linked_contacts` keeps
its own value; nothing renders it, and emptying it would change what the key
means rather than what it holds.

⚠️ **UNTIL PHASE 7 RUNS, FLIPPING AN ACCOUNT HIDES ITS PRE-CUTOVER LINKS** on
the contact and company detail pages, which read one type. That is why the
flag is per-account and the migration is not optional.

Verify: `check_invariants.py`, `check_crm_contract.py`, and a grep that every
`person_entity` caller has `unified` bound.

## Phase 7 — migration — WRITTEN, NOT RUN

`scripts/migrate_contact_unification.py` — `--dry-run` / `--apply`, with
`--account` so it is run **with the flag, one account at a time**, not across
the fleet.

⚠️ **NOT EXECUTED AGAINST ANY DATABASE.** It has never touched data. The dev
VM has no motor/bson, so what is verified below is the logic that could be
isolated; the rest is unrun code and should be treated as such until a dry run
on a restored copy says otherwise.

### Order and resumability

map → profiles → associations (deduped) → task/note arrays.

Resumable through **one marker**: step 1 stamps `migrated_contact_id` on the
`crm_contacts` row and every later step builds its map by reading it. A second
run resolves nothing again — and re-resolving is the thing that could attach a
person to a *different* contact the second time.

⚠️ **NOTHING IS DELETED.** `crm_contacts` is left exactly as it is; that is the
rollback. Turn the flag off and the old path reads the old rows.

### Hazard 1 — the `contacts.email` index · **gated**

Checked before step 1 and the apply **refuses** if a CRM person with no email
would have to be inserted into a unique-but-not-partial index. The script never
alters an index — that belongs to whoever owns the platform collection.

Verified against seven index shapes: unique+partial → safe, unique+sparse →
safe, unique plain → **blocked**, non-unique → safe, a compound index that
isn't this one → safe, no indexes → safe, and **a failed index read → blocked**
(it fails closed; a migration that cannot see the constraint does not get to
assume it).

### Hazard 2 — association collisions · **handled**

A deal linked to BOTH the platform contact and the CRM contact for one person
becomes the same pair twice, which `uniq_crm_association_pair` refuses. The
rewrite looks for the twin first and **drops** the `crm_contact` row instead of
rewriting it — the `contact` row already carries the link. Counted separately
and reported; a high count is not an error, it means those people were linked
from both sides, which is what the unification is for.

### Two things found while writing it

- ⚠️ **Two `crm_contacts` rows in one account can map to ONE person** — the
  same address stored twice before the unique index was partial, or a promote
  plus a manual create. Step 2 therefore **upserts on `(created_by,
  contact_id)` with `$setOnInsert`**, so the second merges into the first
  profile rather than colliding, and a re-run cannot overwrite a profile
  somebody has since edited.
- ⚠️ **The task/note arrays are deduplicated on remap** for the same reason: a
  task linked to both rows for one person would otherwise end up holding the
  same id twice.

### Before running it

1. `--dry-run` on a restored copy; commit the output with the change.
2. Read `BLOCKED (no email)` — non-zero means hazard 1 is live for that account.
3. `--apply --account <id>` for the account whose flag you are about to turn on.
4. Then set `crm_settings.unified_contacts`.

## Phase 8 — UI ✅

**Shipped 29 Oct.** ¹ Campaign engagement is **not** included — see below.

### Marketing state on the CRM record

`ContactDetails.marketing` → `{subscribed, subscribe_date, list_count}`,
rendered as a panel in the contact's right rail.

- ⚠️ **Read straight off the person, never cached on the profile.** Consent is
  the one thing that must not be served stale: a rep deciding whether to mail
  somebody needs today's answer, not the reconcile job's. It costs nothing —
  the contact row is already in hand.
- ⚠️ **`null` on the legacy path, and the panel does not render.** A legacy
  CRM contact has no marketing record, which is NOT "not subscribed". An
  unsubscribed chip there would assert a consent state for somebody the
  audience has never heard of.
- Both payload builders still emit **the same 25 keys** — `payload()` emits
  `marketing: None` rather than omitting it, so the two cannot disagree on
  shape.
- `list_count`, not list names: the CRM has no business rendering another
  module's lists, and the number answers the question a rep has.

### Decision 0.4, said out loud

The panel states it: *"Subscription is managed in Audience. Archiving this
contact does not change it."* Two actions, two places — a rep tidying their
pipeline must not believe they have unsubscribed somebody, **or that they
haven't**.

### The "All contacts" toggle

A `Your contacts` / `All contacts` select on the list, wired to phase 4b's
`scope`. `buildContactParams` **omits `scope` when it is the default**, so a
legacy request is byte-identical to what it was before this existed — pinned
by two tests (416 UI tests pass).

⚠️ The control does **not** pre-empt the server's refusal of CRM filters on
`all`. The set of refused filters is the server's to define, and a second copy
in the client would drift from it.

### Not included: campaign engagement

Brevo's contact record shows sent / delivered / opened / clicked. Those live in
campaign and event collections, not on the contact row, so surfacing them needs
a **seventh host read** — the same deliberate step as `contacts_page` in phase
4b. Left out rather than half-built. The panel as shipped answers the question
that actually blocks a rep ("may I email this person?"); engagement answers
"what happened when we did", which is reporting.

## Phase 9 — retire `crm_contacts` — BLOCKED

⚠️ **NOT STARTED, AND IT SHOULD NOT BE.** Phase 7 has never run against a
database. Phase 9's whole risk is that it removes the rollback — and the
rollback is the untouched `crm_contacts` collection plus the code that reads
it. Removing either while the migration is unproven means a bad cutover has
nowhere to go back to.

What shipped instead is the gate.

### `scripts/check_unification_complete.py`

Five questions, each one a way phase 9 could destroy something:

| | check | what it catches |
|---|---|---|
| 1 | every account on the unified path | a flag still off, or the env kill switch on — those accounts are *running* the code phase 9 deletes |
| 2 | every `crm_contacts` row has `migrated_contact_id` | people who exist only in the collection about to be dropped |
| 3 | no association with `to_type="crm_contact"` | links that would orphan |
| 4 | no task or note holding a `crm_contacts` id | **the quietest failure in the plan** |
| 5 | every profile resolves to a person | a migration that half-ran |

⚠️ **Check 4 exists because that failure is invisible.** `crm_tasks.contact_ids`
and `crm_notes.contact_ids` carry no type and are validated against nothing —
a stale id renders a blank contact chip and raises no error, ever. It is found
by asking whether any id in those arrays is a `crm_contacts` `_id`, which is
the only way to see it.

⚠️ **It reads and reports; it writes nothing.** A check that could repair what
it is checking is a check nobody trusts.

⚠️ **Exit 0 means "nothing still depends on `crm_contacts`" — not "go".** It
proves the data is ready. It cannot prove the new path has been lived with,
which is a judgement about how long accounts have run unified without
complaint, and belongs to a person. The script says so on success rather than
implying otherwise.

One note it raises deliberately: if the **env override** is what is forcing
accounts unified, phase 9 removes *both* switches, so each account's own flag
must be true as well — otherwise removing the override silently reverts them.

### What phase 9 will do, when it is unblocked

1. Drop `CrmContactModel` from the registry; delete the model and its CRUD.
   (`promote` went in phase 5.)
2. Remove the flag, the env kill switch and `contact_scope.unified` — a dead
   branch is how the old path comes back.
3. Drop the collection **in a release of its own**, after the code change, so
   a rollback in between still has its data.
4. Regenerate `docs/api-contract.md --emit` and re-run both checkers.

## Running order

```
0 ─ decisions
    └─ 1 ✅ ─┬─ 2 ─┐
             └─ 3 ─┴─ 4 ─ 5 ─ 6 ─ 7 ─┬─ 9
                        └─── 8 ──────┘
```

2 and 3 can run in parallel with each other. 8 can start as soon as 5 lands and
does not have to wait for the migration. Nothing after 3 should ship with the
flag on for more than one account until 7's dry run is clean.
