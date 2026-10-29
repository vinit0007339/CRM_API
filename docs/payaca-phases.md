# Core CRM plan — pipeline, company, deals, customer flow

**Plan only. Nothing implemented.**

Scope narrowed on request: **pipeline, company, deals and the customer flow.**
Everything else from `payaca-gap-analysis.md` is parked, with the reasons in §4.

Replaces the earlier six-phase version of this document.

---

## 0. Decisions taken

Recorded so they are not re-litigated.

| Question | Answer |
|---|---|
| Is quoting/invoicing ever in scope? | **No, never.** Engage bills our customers; it will not help them bill theirs. Payaca's customer-value split has no source data here and is permanently out. |
| Who owns `flow_scheduler.py` / `custom_flows`? | **Nobody right now.** CRM-417 cannot be signed, so Epic 30 is **parked**, not pending. |
| @mentions notification path | **Dropped entirely.** |
| Communications: automatic or pinned? | Moot — communications is out of the narrowed scope (§4). |

---

## 1. Walking the flow, step by step

The scope is a flow, so the plan is written as one. Each step is marked with
what actually happens today.

| | Step | Today |
|---|---|---|
| 1 | An enquiry arrives; create the **company** | ✅ 16 endpoints, auto-link by email domain, merge + dedupe |
| 2 | Put a **person** on it | ⚠️ the button exists, `POST /crm/contacts` **500s** |
| 3 | Move them **lead → qualified → customer** | ❌ display only |
| 4 | Create a **deal** for them | ❌ no way in from the company page |
| 5 | Move it **through stages** | ✅ Kanban board, drag-and-drop, per-stage weighted values |
| 6 | **Close** won or lost, with a reason | ✅ `/close`, `/reopen`, `lost_reasons` in settings |
| 7 | See **what happened** | ✅ activity timeline with field-level diffs |
| 8 | **Report** on it | ❌ nothing reads any of it back |

Five of eight work. The three that do not are steps 2, 3, 4 — consecutive, and
all in the middle of the flow — plus step 8 at the end.

That split is the plan: **phase 1 is steps 2–4, phase 2 is step 8.**

---

## Phase 1 — make the flow work end to end

No new endpoints. `EXPECTED_ENDPOINTS` stays **91**, routers stay **13**. Every
API this phase needs already exists; what is missing is a working database and
two ways in from the UI.

### 1a. Step 2 — unblock `POST /crm/contacts`

The 500 you hit. Root cause is the stale `uniq_crm_contact_email` (built
`sparse=True`) on the local `crm_contacts`, which the model now declares
partial. `allow_index_dropping` is `False` by design, so Beanie will not
resolve it.

**Needs your database, not code:**

```bash
python3 scripts/check_crm_contact_index.py          # report
python3 scripts/check_crm_contact_index.py --apply  # one drop_index, no document writes
# restart, then:
curl -s localhost:8100/health/ready | jq '.models'
```

`models.registered: false` means `init_beanie` failed at startup and every
model operation fails. `true` plus a 500 only on the *second* no-email contact
means the index is still sparse. The create path now reports which of the two it
is rather than returning a bare 500.

### 1b. Step 3 — a contact's lifecycle must be movable

**What is missing:** `ContactDetails.jsx` renders `lifecycle_stage` as a chip
and nothing more — lines 294-295 are the only mention. Moving someone from
`lead` to `qualified` means opening the full edit dialog and saving the whole
record.

For a flow whose whole point is a person progressing, that is the central
action behind the most friction.

**What it needs:** a stage control on the detail page and on the list row —
`PATCH /crm/contacts/{id}` with one field. The endpoint exists; `LIFECYCLE` is
already the single source of truth in `shared/contactHelpers.js`, pinned by
four tests and the cross-repo invariant.

⚠️ **Use `LIFECYCLE_STAGES` from `contactHelpers`, do not retype the list.**
That duplication was live until this week: the UI offered seven stages, five of
which the server rejects, and could not reach `subscriber` or `churned` at all —
including `subscriber`, the stage every imported contact would carry.

⚠️ **The order is the funnel's**, not alphabetical: subscriber → lead →
qualified → customer, churned last. Pinned, because sorting it puts `churned`
second.

**Open question for you:** should the control allow any stage, or only forward
plus `churned`? Free movement is simpler and honest about correcting mistakes;
a guided path reports better. `contact_rules.clean_lifecycle` enforces
membership but not direction, so either is a UI decision, not a server one.

### 1c. Step 4 — a deal must be creatable from a company

**What is missing:** `CompanyDetails.jsx` has no deal-create entry at all —
grepped for `DealFormDialog`, "New deal", "Add deal": nothing. So the
company → deal step means leaving the company, opening Deals, creating one, and
picking the company back out of a dropdown.

This is the break that costs most. It is the moment the flow is *for*, and it is
the one step that sends the user somewhere else to do it.

**What it needs:** a **+ New deal** button on the company page opening
`DealFormDialog` with the company pre-filled, exactly the shape "+ New contact"
now has. `POST /crm/deals` already accepts a company; `crm_associations` already
carries company↔deal.

⚠️ **One association, not two.** A deal created here must produce a single
`company → deal` row. `_mutate_links` is the existing path and
`normalize_pair` enforces the canonical direction; writing the link by hand in
the dialog's success handler is how a duplicate appears that the unique index
then rejects on the next attempt.

⚠️ ~~**`is_primary` exists on the association** and marks the company shown on a
deal's Kanban card. A deal created from a company page should set it.~~
**CORRECTED WHILE BUILDING THIS — the guidance above was wrong.** `deals.py`
says it outright at two places: *"the association's `is_primary` flag is written
but nothing reads it"*. The authoritative value is the **deal column**
`primary_company_id`, and `deals.py:971` computes the displayed flag by
comparing against that column rather than reading the row. `create_deal` sets
the column and leaves the row's flag `False`, which is correct. A test asserting
the row's flag tests a field the product does not use — mine did, and that is
how this was found.

### Phase 1 status — 1b and 1c APPLIED, 1a outstanding

| | State |
|---|---|
| **1a** drop the stale index | ⏳ **yours** — needs a database this session cannot reach |
| **1b** lifecycle control | ✅ applied, free movement, options from `contactHelpers` |
| **1c** + New deal on the company | ✅ applied, one association, no new endpoint |

**Endpoints unchanged: 13 routers, 91.** Phase 1 added none, as predicted.

#### The flow, walked server-side

All eight steps, one pass, through the real routes:

```
1 pipeline + default stages · company created
2 contact created · company attached and named
3 lead -> qualified -> customer
4 deal created from the company · primary_company set
5 two stage moves accepted
6 closed won
7 deal activity types: ['created', 'stage_changed', 'won']
```

Steps 5–7 had not been verified before this; they needed no change.

#### Three test assertions were wrong, not the code

Recorded because the pattern is the lesson, not the individual mistakes:

* **`is_primary` on the association** — asserted True; it is False and that is
  correct. `deals.py` says it twice: *"the association's `is_primary` flag is
  written but nothing reads it."* The deal COLUMN `primary_company_id` is
  authoritative and `deals.py:971` computes the displayed flag from it.
* **`primary_company_name` in the response** — that is the denormalised
  column's name. `contact_rules.payload` surfaces it as a nested
  `primary_company: {id, name, domain}` ref.
* **a pipeline seeded with explicit stages** — refused with *"Two stages are
  both called Won"*, because create provisions defaults. Correct behaviour.

In each case the first instinct was that the code was wrong. Worth keeping in
mind when reading any of the ⚠️ notes in this document: they were written
before the code was exercised.

### Acceptance for phase 1

Walk the whole flow in the UI without leaving it: create a company → add a
person → move them to `qualified` from the detail page → create a deal from the
company → drag it two stages → close it won → see all six events on the
timeline. No 500s, no navigating away mid-flow.

`/health` → `13/17/91` unchanged, contract PASS, invariants PASS.

---

## Phase 2 — pipeline reporting (step 8)

The only Payaca gap that survives the narrowed scope, and it is in scope twice
over: it is about pipelines and deals.

### Why it is cheap — measured, not assumed

`CrmActivityModel` carries `entity_type`, `entity_id`, `activity_type`,
`actor_user_id`, `occurred_at` and a free `metadata` dict. And
`activity_recorder.stage_changed` already writes:

```python
metadata={"from_stage_id": ..., "from_stage_name": ...,
          "to_stage_id": ...,   "to_stage_name": ...}
```

So the full stage-transition series with timestamps **already exists for every
deal**. Time-in-stage is a window over consecutive rows; conversion is
transitions out of a stage over deals that entered it. Lost reasons are already
collected into `crm_settings.lost_reasons` and recorded on close, and nothing
reads them back.

**No new writes. No migration.** This phase only reads.

### New files

| File | Contents |
|---|---|
| `app/routes/reports.py` | the router, 4 endpoints |
| `app/services/reporting.py` | the aggregations, pure where possible so they test without a database |

### Endpoints (4) → 95 endpoints, 14 routers

| Method | Path | Permission |
|---|---|---|
| `GET` | `/crm/reports/pipeline` | `deals:read` |
| `GET` | `/crm/reports/conversion` | `deals:read` |
| `GET` | `/crm/reports/time-in-stage` | `deals:read` |
| `GET` | `/crm/reports/lost-reasons` | `deals:read` |

All take `pipeline_id`, a date range and an optional `owner_id`.

`deals:read`, not a new permission: `PERMISSIONS` stays at 32 so
`check_invariants.py` needs no change, and a permission no role grants is an
endpoint nobody can reach — the orphan check already reports 8 of those.

### ⚠️ Four traps

**A deal that never moved has no `stage_changed` row.** Computing time-in-stage
from transitions alone silently omits every deal still sitting in the stage it
was created in — usually the worst dwell time and the exact thing the report
exists to find. Seed the series from the deal's `created_at` and its creation
stage.

**Reopened deals re-enter a stage.** `reopened` writes its own activity, so a
deal can be counted twice in "entered stage X" unless transitions are walked in
`occurred_at` order per deal rather than counted flat.

**`actor_name` is a deliberate snapshot** — it survives user deletion. Group
win-rate on `actor_user_id` and only *display* `actor_name`, or a renamed user
splits into two rows.

**Archived deals.** Decide explicitly whether they count. A conversion rate that
silently drops archived deals reads better than reality.

### The standing tax

A new router means: `_MOUNT_TABLE`, the import, a **literal** gated
`include_router` (the contract checker reads these by AST; a loop broke it once,
and an unreadable prefix is reported at a guessed `/crm/<module>`),
`EXPECTED_ROUTERS = 14`, `EXPECTED_ENDPOINTS = 95`, `profiles.py` plus a
`core`/`minimal` decision, and `docs/api-contract.md` regenerated with
`--emit`, which supplies its own markers — replace from the first opening marker
to the last closing one.

### Acceptance

`/health` → `14/95`, contract PASS. Against a hand-counted fixture: conversion
percentages reconcile; a deal created and never moved appears in time-in-stage;
a reopened deal is counted once per entry.

### ✅ Done — and four more traps than the four above

`app/services/reporting.py` + `app/routes/reports.py`, 4 endpoints.
`/health` → `14 routers · 18 mounts · 95/95`, contract PASS, invariants PASS,
the harness green, and the six earlier phase harnesses still green.
`reports` is in `ALL_MODULES` and in the `full` profile only — **not** `core`;
a `core`/`minimal` decision is still open.

The four traps the plan named were real. Four more were not in the plan and
each one silently produced a plausible wrong number — worth recording, because
every one of them comes from the same place: **`close_deal` overwrites
`stage_id` and `stage_entered_at`, and records no stage activity.**

| | What it did | Fix |
|---|---|---|
| `DealStatus` is an enum | `str(deal.status)` is `"DealStatus.WON"`, which matches none of `won`/`lost`/`open` — so win rate, conversion and lost reasons all reported **zero** while `/pipeline`, which reads `.value`, was right. Two copies of one rule again. | `reporting._status()`, one value-aware read the aggregations share |
| every loss landed on the **Lost** column | attribution used the deal's own `stage_id`, which close had already set to Lost. The report said deals die in the Lost stage: true, and worthless. | `reporting.closing_stage()` — the last **open** stage, from the transition series |
| closed deals **entered** the Lost column | same cause, on the entry side: a never-moved closed deal's "creation stage" read as Lost, so stage one lost exactly the deals that never left it | same helper, plus `unattributed` for deals whose creation stage is genuinely not in the data |
| the Won column accrued dwell **forever** | `now - stage_entered_at` on a closed deal measures *days since it was won*; meanwhile the spell that matters — time in the last open stage before the close — was dropped, because `zip(path, path[1:])` never closes out the final entry | a close **ends** a spell: `actual_close_date` terminates it, and nothing starts in a terminal column |

Two more, smaller:

* **`reopen` records `activity_type="reopened"`, not `stage_changed"`.** The
  plan's own trap #2 said a reopened deal could be counted *twice*; in fact it
  was counted **not at all**, because the route queried one activity type.
  `reporting.MOVEMENT_TYPES` is now the set, and `_gather` queries it.
* **A terminal stage cannot have a conversion rate.** Nothing advances out of
  Won or Lost, so the rate was a guaranteed `0.0%` printed beside the deals
  that actually closed there — the exact misreading the `None`-not-zero rule
  exists to prevent. `conversion_rate` is `None` there, and rows now carry
  `stage_type` so a null is readable.

⚠️ **`include_archived` was planned and is not there.** `DealModel` has no
`is_archived` and `DEAL_ACTIONS` has no `archive` — deals cannot be archived in
this codebase. The parameter would have matched zero deals and read as
"archived deals excluded". Trap #4 in the list above is therefore answered: the
question does not arise. (Pipelines *do* have `is_archived`; deals do not.)

⚠️ **One thing reporting cannot recover.** A deal created and closed without
ever moving has no stage anywhere in the data: `stage_id` was overwritten and
`activity_recorder.created` records only `name` and `source`. Those deals come
back as `unattributed`, not folded into stage one. If that count is ever large,
the fix is upstream — `created` should record the creation stage — and it only
helps deals created after the change, the same way the recorder's own docstring
says.

---

## Phase 3 — the reporting UI (step 8, the other half)

Not in the original plan, because the plan treated step 8 as "report on it" and
Phase 2 answered it with four endpoints. From a user's seat it was still ❌: no
screen read them, so the reports existed only to `curl`.

### ✅ Done — UI only, no API change

`src/screens/CRM/Reports/`, route key `crmReports`, path `/crm/reports`, menu
item under CRM gated on **`deals:read`** — the same grant the Deals screen
needs, so no new permission and `PERMISSIONS` stays at 32.

| File | |
|---|---|
| `index.jsx` | the shell: pipeline select, cohort range, owner filter, one fetch |
| `ReportSection.jsx` | the frame all five sections share — loading, empty, caveat |
| `PipelineHealth.jsx` | the four tiles + the weighted total |
| `StageFunnel.jsx` | per-stage entered / still here / moved on / won / lost / rate |
| `TimeInStage.jsx` | median bar, with average, longest and spell count beside it |
| `LostReasons.jsx` | distribution by count and value |
| `WinRateByOwner.jsx` | won against closed, per owner |
| `reportHelpers.js` + `.test.mjs` | the pure parts, 16 tests |

Four endpoint constants in `constant/Environment.jsx` and four
`unwrap()`-funnelled helpers in `Utility/crm.js`. Wiring in `routeMap.js`,
`screens/index.js`, `features.js` and `menuConfig.js`.

### The decisions that are not cosmetic

**One screen, not four tabs.** The four reports answer one question between
them and share every filter. Tabs would let a reader compare a conversion rate
from one cohort against a win rate from another without noticing. One filter
bar, one `Promise.all`, one state update — so the screen never shows three
fresh reports beside one stale one.

**"Created in the last 30 days", not "Last 30 days".** The server's window is
on `created_at` — a cohort — and a reader cannot work that out from the
numbers. Getting it backwards inverts the conclusion, so every preset label
says the word *created*, and a test asserts that it does.

**No "all pipelines".** Unlike the Deals list: conversion and dwell are
per-stage, and stages belong to one pipeline, so "all" would sum columns that
are not the same column.

**Null never renders as zero.** `conversion_rate: null` now means two different
things — a terminal column where the rate cannot apply, and an open stage no
deal reached where it is not yet known — so `rateCell()` returns `n/a` for the
first and `—` for the second, each with its own tooltip. Same rule for a win
rate with nothing closed, and for a stage with no dwell samples: "no closed
deals yet" and "lost every deal" are opposite findings, and `0%` reads as the
second.

**An owner we cannot name is described, not printed.** `win_rate_by_owner`
groups on `owner_id` and carries no name (the server's note explains that
`actor_name` is a snapshot, so grouping on it splits a renamed person in two).
Resolving it needs `team:read`, which a CRM-only role does **not** have — so a
real user with `deals:read` sees this report and no names at all. That renders
as `Teammate · ab12cd` plus one line saying why, never a 24-character
ObjectId in a table cell.

**The caps are stated.** `scope.truncated_deals` / `truncated_activities`
become a warning above the numbers, not a footnote: a conversion rate quietly
computed over the first 5,000 of 12,000 deals looks exactly like a right one.

**Tables and bars, no pie.** Three slices of open/won/lost are harder to read
than the three numbers; the bars are where comparison between rows is the
point. Single series throughout, so no legend — the column header names it.

### Verified

eslint `--no-ignore` exit 0 on all 15 touched files; 16 helper tests pass; and
a contract harness (`/tmp/shape.py`, against the live routers) asserts that
**every field the screen reads exists in the four responses** — including
`scope.truncated_*`, `stage_type`, `conversion.unattributed`,
`lost_reasons.labels_available` and `currency`/`is_default` on the pipeline
rows the selector needs.

⚠️ **Not verified in a browser.** `/crm/reports` resolves and the app compiles
clean, but the pane redirected to `/login` and signing in is not something to
do on your behalf. The render pass is yours — and worth doing deliberately,
because `no-undef` is **off** in this repo's eslint config, so a lint pass
cannot prove a JSX element resolves. That is the `Avatar is not defined` class
of bug; I checked all 63 JSX elements and every imported symbol against their
modules by hand instead, which is a good substitute and not a complete one.

---

## Phase 4 — the filter bar and saved views, un-hidden

### What it actually was

Not unfinished work. `git log -S` found one commit: **`b68a0d4` ("add",
23 Oct)**, which wrapped five blocks in `{/* … */}` — the diff's own label is
`{/* hide button`. Everything behind them was left intact: state, handlers, the
serialised filter already travelling in the fetch params, and the dialogs still
mounted. The 12 lint errors were the symptom, not the problem.

| Screen | Hidden in b68a0d4 | Now |
|---|---|---|
| Companies | SavedViewBar, FilterBuilder | ✅ restored |
| Companies | Duplicates, Import, Export buttons | still hidden — not asked for |
| Deals | SavedViewBar, FilterBuilder (list view only) | ✅ restored |
| Deals | Import, Export buttons | still hidden — not asked for |

### Checked before restoring, not after

A restored control that 404s is worse than a hidden one, so the endpoints came
first (`~/h/filters.py`): all three `/filter-fields` catalogues answer,
`/crm/saved-views` POST + GET + PATCH + DELETE all work, and both list
endpoints accept `?filters=`.

Two things that harness turned up, neither a defect:

* **Operators are validated per field.** `{"field":"status","operator":"eq"}`
  is refused — *"Status cannot be filtered with eq"*. That is the catalogue
  doing its job: `filter_rules.field_catalogue` is the only vocabulary the
  builder renders from, so a user cannot compose a condition the server would
  then refuse. A hand-written payload has to respect it too.
* **`/crm/saved-views` nests its list** — `data: { data, total,
  default_view_ids, max_views }`, unlike every other CRM list.
  `crm.js::listSavedViews` already passes `{ nested: true, extras: [...] }` to
  `unwrap()` for precisely this, so the screen sees a flat array. Worth knowing
  the next time something reads that endpoint raw.

And one coherence check on the restored wiring: Companies' `applyFilter` resets
`page` to 0, while Deals passes `setFilterGroup` straight through — which is
correct, because `DealsListView` owns its own paging and already resets it on
`filtersParam`. Without that, a narrower filter strands the reader past the
last page and the table renders empty with a non-zero count.

### The five remaining lint errors

Gone, but **not** by deleting the icons. `ContentCopy`, `FileDownload` and
`FileUpload` are the icons of the buttons you hid, and the buttons are
commented out rather than removed — so an `eslint-disable no-unused-vars` block
with the reason and the commit sha keeps them, where deleting them would make
restoring those buttons a two-file change again. The repo's own convention for
an intentionally-kept unused symbol.

`eslint --no-ignore src/screens/CRM/` is now **exit 0**, from 12 errors. 361
helper tests pass (`TZ=Asia/Kolkata` — the task-helper suite refuses to run
under UTC on purpose).

⚠️ **Render pass is still yours**, same reason as phase 3.

---

## 3. Order, and what it costs

| | Work | Endpoints | Why here |
|---|---|---|---|
| **1a** | drop the stale index | 91 | your database; nothing else can be tested until step 2 works |
| **1b** | lifecycle control | 91 | UI only, the endpoint exists |
| **1c** | + New deal on the company page | 91 | UI only, the endpoint exists |
| **4** | filter bar + saved views un-hidden | 95 | ✅ done — UI only, your own b68a0d4 reversed in part |
| **3** | the reporting UI | 95 | ✅ done — UI only, no new endpoints |
| **2** | pipeline reporting | **95** | ✅ done — the only surviving Payaca gap in scope |

Phase 1 adds **no endpoints at all** — it is one database fix and two buttons.
That is worth saying plainly: the core flow is not missing an API, it is missing
two entry points and a working index.

---

## 4. Parked, with reasons

| | Why |
|---|---|
| **Automations / per-stage triggers** | CRM-417 is written and cannot be signed — nobody owns `flow_scheduler.py`. Epic 30 (CRM-418/419/420) is **parked, not pending**. The CRM's own half is small and stays unbuilt until there is an engine owner. |
| **Communications on the record** | Out of the narrowed scope. Worth knowing it is cheaper than it looks when it returns: `EmailThread` already carries `contact_id`, `contact_address` and `match_method`, and `campaign_delivery_logs` is the outbound half — but it needs two new read delegations through `foreign.py` and the host adapters. |
| **@mentions** | Dropped. No notification route, no notification model, and the push service's stage config is incomplete. |
| **Invoicing, customer value by invoice state** | Never in scope, decided. |
| **AI query layer** | Needs the event contract first. |
| **Map scheduling, field app, proposals, solar/heat-pump design, DNO, service plans, portals** | Trades-specific. Not our product. |

---

## 5. Two things that still need you

1. **Run the index fix** (1a). Nothing in phase 1 is testable until
   `POST /crm/contacts` works, and it needs a database I cannot reach.

2. **Lifecycle movement: free or forward-only?** (1b). A UI decision —
   `clean_lifecycle` enforces membership, not direction.

And the standing one, unchanged: the contacts work across all three repos is
uncommitted. Phase 1 builds directly on it.
