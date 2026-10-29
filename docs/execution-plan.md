# Execution plan — phase by phase

Derived from `docs/remaining-work.md` (the inventory) and
`docs/lead-intake-plan.md` (the design). This document is the **order of
operations**: what to do, in what sequence, who does it, and what proves it
worked.

Nine phases. Phase 0 is yours and gates everything; phases 1–3 are each small
and independently useful; 4–7 are the lead → pipeline feature; 8 is hygiene.

---

## Ground rules

1. **I do not commit or push. Ever.** Every phase ends with a `git status`
   review and **you** committing. If a phase is half-done, it stays
   uncommitted — I will not tidy it into a commit to make it look finished.
2. **One commit per phase, not per file.** Each phase below is scoped so its
   diff is one coherent change you can review in a sitting and revert as a
   unit.
3. **A phase is not done until its verification gate passes.** Every phase has
   one. If a gate fails I stop and report rather than moving on.
4. **Decision gates are marked 🔸.** A phase with one cannot start until you
   answer; I will not guess and build.
5. **Nothing touches your database or your server.** Your standing rule —
   *"we can not do anything on the server direclty we can just add the env"* —
   holds. Where a step needs the database, it is yours and marked so.

---

## Phase 0 — land what is finished, and unblock the rest ⟵ **start here**

**Owner: you.** No code from me. This exists as a phase because everything
after it is untestable until it is done.

| | Step | How |
|---|---|---|
| 0.1 | Drop the stale index | `python3 scripts/check_crm_contact_index.py` (report), then `--apply`, then restart |
| 0.2 | Confirm the models registered | `curl -s localhost:8100/health/ready \| jq '.models'` → `registered: true` |
| 0.3 | Review the uncommitted diff | `git status` in `CRM_API` and `emailMarketingUI` — the inventory lists every file |
| 0.4 | Browser pass | `/crm/reports`, plus the filter bar and saved views on `/crm/companies` and `/crm/deals` (list view) |
| 0.5 | **Commit and push** both repos | yours |
| 0.6 | Rotate the `internal_key` and the `admin:admin` `db_uri` | both were exposed in this session's transcript |

⚠️ **0.1 is the gate.** `POST /crm/contacts` 500s until it runs, so the
contact → company → deal flow cannot be walked and no later phase can be
verified against real data.

⚠️ **0.4 matters more than it looks.** `no-undef` is **off** in this eslint
config, so a lint pass cannot prove a JSX element resolves — that is how
`Avatar is not defined` shipped. I checked all 63 elements against their
imports by hand; it is a good substitute, not a complete one.

🔸 **Decision:** does `reports` belong in the `core` / `minimal` profile?
Currently `full` only, reasoning at `app/profiles.py:76`.

**Gate:** `/health` → `14 routers · 18 mounts · 95/95`; `check_crm_contract.py`
PASS; `check_invariants.py` PASS; `POST /crm/contacts` returns 200.

---

## Phase 1 — close the public docs surface

**Owner: me. Size: three lines.** Highest risk-to-effort ratio in the whole
plan, and unrelated to everything else — which is why it is first.

| | Step | File |
|---|---|---|
| 1.1 | Uncomment the three gated arguments | `emailMarketingAPI/api/main.py:410-424` — `docs_url`, `redoc_url`, `openapi_url`, each `if _docs_on else None` |
| 1.2 | Confirm `_docs_on` is then actually read | same file; it is computed and currently unused |
| 1.3 | Apply the same gate to the CRM | `CRM_API/app/main.py` — the service has no docs settings at all, so `/docs` is open in every environment |
| 1.4 | Check the other factory still agrees | `api/maincon.py:358-372` already has it right — leave it alone |

**Gate:** with `app_env=PRODUCTION`, `/docs`, `/redoc` and `/openapi.json`
return 404 on both services; in dev they still work. Then **you commit.**

⚠️ `dev_api.py:12` and the process manager boot `api.main:app` — the ungated
one — which is why the correct gate in `maincon.py` has never protected
anything.

---

## Phase 2 — the API key path into the CRM 🔸

**Owner: me. Size: one conditional plus tests.** This is the short road to
*"get the data into a specific deal or pipeline"*.

🔸 **Decision first: does your customer's website have a backend?**
If yes, this phase delivers the feature and **phases 5 and 6 become
optional.** If their site is static HTML/JS, a key cannot be held safely —
skip to phase 3 and the forms path.

| | Step | File |
|---|---|---|
| 2.1 | Skip the local JWT verify for a key — when the bearer starts with `eng_live_`, go straight to introspection | `CRM_API/app/auth/context.py:127-135` |
| 2.2 | Keep the failure mode identical for everything else — one `AuthError("Invalid Credentials", 401)`, no new message that tells a prober which check failed | same |
| 2.3 | Confirm the scope clamp lands — a key's scopes arrive as `extra_grants` and `rbac.has_permission` unions them in | verify, do not change |
| 2.4 | Test matrix: a key with `deals:write` creates a deal; a key **without** it gets 403; a malformed `eng_live_…` gets 401; a real JWT still works unchanged; an unreachable platform still returns **503, not 401** | harness |
| 2.5 | Write the two-call recipe into `docs/` — company by name → deal with `pipeline_id` + `stage_id` + `contact_ids` | docs |

**Why it is this small:** the platform's `GET /api/v1/internal/auth/context`
already resolves a key and returns `is_api_key`; the CRM already forwards the
raw header; `context.py:171` already sets `api_key=object() if
resolved.is_api_key else None`. Only the local RS256 pre-check stands in the
way.

⚠️ **The 503 case in 2.4 is not optional.** The CRM deliberately answers 503
rather than 401 when the platform is unreachable, so a platform blip does not
sign every user out. A change in this function is the easiest place to break
that.

🔸 **Decision:** API keys are gated behind `require_api_keys_enabled` or a
Workspace plan carrying `engage-api-access`. Is "use the API" something an
Engage customer can buy? That is commercial, not technical, and phase 2 is
inert without it.

**Gate:** the 2.4 matrix passes; `check_crm_contract.py` PASS (no endpoint
change); existing auth behaviour unchanged. Then **you commit.**

---

## Phase 3 — make form capture reach a list

**Owner: me. Size: small.** Independent of phase 2; needed only if you use
Landing Pages / Signup Forms. Two cheap fixes, no decisions.

| | Step | File |
|---|---|---|
| 3.1 | Refuse to publish a form or page with no list, **server-side** | `content_builder.py:1247-1261` (`/status` has no check at all today) |
| 3.2 | Warn in the editors where the list is still optional | `SignupFormEditor.jsx`, `LandingPageEditor.jsx` |
| 3.3 | Fire the trigger on opt-in confirm — route the raw membership upsert through `ListMembershipCrud.add_contact` | `content_builder.py:941-962` |
| 3.4 | Confirm the double membership-write rule now has one home | the confirm path currently duplicates the CSV importer's write |

⚠️ **3.1 is the silent one.** With no list, a lead confirms, joins nothing,
appears in no list view and triggers no flow — **ever** — while the visitor
reads "Thanks for subscribing!". A deal becomes impossible because of an empty
dropdown on a different screen.

⚠️ **3.3 is latency, not loss.** The scheduler sweep picks these contacts up
within ~2 minutes already; this removes the wait and the second copy of the
rule.

**Gate:** a form cannot be activated via the API without a list; a confirmed
opt-in appears in the list view **and** starts a `list_join` flow within the
request. Then **you commit.**

---

## Phase 4 — split "lead" from "subscriber" 🔸

**Owner: me. Size: medium.** The compliance-shaped one.

🔸 **Decision: should an enquiry form skip double opt-in?** My recommendation:
**yes for CRM intake, no for marketing** — create the deal and let sales
reply, while the contact stays `subscribed=False` and marketing cannot send to
it until it confirms.

| | Step | File |
|---|---|---|
| 4.1 | Call the existing decision function from the four submit paths | `compliance.requires_double_opt_in(enabled=…, source=…)` at `compliance.py:206` — written **and tested**, with no caller |
| 4.2 | When DOI is off: subscribe and join the list on submit | `content_builder.py` ×4 paths |
| 4.3 | Keep `subscribed` and "is a sales lead" as **two** states | model + the submit paths |
| 4.4 | Make the editor toggle mean something, and say which it controls | `SignupFormEditor.jsx:677` |

⚠️ **Collapsing the two states is how a quote request becomes an unconsented
newsletter send.** The toggle exists, is stored, is shown in the UI, and is
read by nothing — so today DOI is unconditional and turning it off changes
nothing. Wiring it is one tested function; deciding what it *means* is 4.3.

**Gate:** with DOI on, behaviour is byte-identical to today. With DOI off, the
contact lands subscribed and listed in one request, and a campaign send still
refuses a contact that has not confirmed. Then **you commit.**

---

## Phase 5 — the CRM's door and the platform's caller 🔸

**Owner: me. Size: medium.** Only needed for the marketing track — skip if
phase 2 covers your case.

🔸 **Decision: identity — Option A, B or C** (`lead-intake-plan.md` §4), and
**the company — i, ii or iii** (§4.1). A + i is my recommendation; both reuse
things already built.

| | Step | File |
|---|---|---|
| 5.1 | Restore `app/routes/internal.py` from `a6e46fb^`, auth half **verbatim** | fail-closed secret, `compare_digest`, 404-not-403, nginx deny, mounted outside `/crm` |
| 5.2 | Add `POST /api/v1/internal/crm/leads`, batch ≤ 5000 | same file |
| 5.3 | Wire the mount — `_MOUNT_TABLE`, `profiles.py`, a **literal** gated `include_router` | `app/main.py` |
| 5.4 | Finish the truncated comment at `app/main.py:479` | it currently stops mid-clause |
| 5.5 | Stamp `CrmRecordSource.AUTOMATION` on what it creates | `app/enums.py:144` — exists, never written |
| 5.6 | Restore `api/services/crm_client.py` and call it after the contact write | config already parsed at `app/__init__.py:554-560` |

⚠️ **This endpoint must never fail the caller's write.** The deleted file
already had the rule: *"a company link that did not happen must not turn into
an error on a request whose real work is done."* A CRM outage skips the step;
it does not 500 a signup form.

⚠️ **`check_crm_contract.py` counts `/crm` only**, so the documented **95
endpoints is unaffected** — but the mount table and `EXPECTED_ROUTERS` still
need the entry, and a non-literal `include_router` breaks the checker's AST
read.

**Gate:** the endpoint refuses without the secret (404) and when the secret is
unset (503); a batch of 5,000 is one company lookup, not 5,000; a CRM outage
leaves the platform's own write succeeding. Then **you commit.**

---

## Phase 6 — the `crm_deal` flow step 🔸

**Owner: me, after an ownership answer. Size: medium.**

🔸 **Decision: who owns `flow_executor.py`?** This is the **only** phase in
this plan that needs somebody else's agreement. It is a *smaller* ask than
CRM-417: additive code, about a contact, keyed on `(flow_id, contact_id)` —
exactly the key CRM-417 §3.2 says cannot express a deal event. Ask it as its
own question rather than bundling it with the stalled one.

| | Step | File |
|---|---|---|
| 6.1 | Add `crm_deal` as the fifth step type | `flow_executor.py:538-647` (currently 4, then `skipped: True` at `:646`) |
| 6.2 | Config model: pipeline, stage, name template, owner, optional task — the §2.2 field list | platform |
| 6.3 | Record the outcome in the journey like any other step | `JourneyStepRecord` at `flow_trigger_log.py:30` already has `status`/`attempt`/`error` |
| 6.4 | Palette entry + config panel | `FlowBuilder/index.jsx:130-139` |
| 6.5 | The panel reads pipelines with the **user's own token**, needing `pipelines:read` — say so rather than showing an empty dropdown | UI |

⚠️ **No dedupe table is needed, and that is worth knowing before designing
one.** `FlowTriggerLog` has a unique index on `(flow_id, contact_id)`, so our
engine cannot re-enrol a contact in a flow — one contact, one flow, one deal,
enforced by an index that already exists. Brevo creates a duplicate on every
re-entry; we get the better behaviour for free. **If flow re-entry is ever
added to the engine, that guarantee disappears with it.**

**Gate:** a contact joining the trigger list gets exactly one deal in the
configured pipeline and stage; a CRM failure marks the step failed and the
contact's automation continues; re-running the scheduler creates no second
deal. Then **you commit.**

---

## Phase 7 — promote a lead to a CRM contact

**Owner: me. Size: small.** Depends on phase 5.

| | Step | File |
|---|---|---|
| 7.1 | A button on a deal whose person is a platform contact | `DealDetails.jsx` |
| 7.2 | Create the `crm_contacts` row from the platform contact | CRM |
| 7.3 | Re-point the association `contact` → `crm_contact` | via `_mutate_links` |

⚠️ **Delete-and-insert, never an in-place `to_type` edit.** `normalize_pair`
owns the canonical direction and `AssociationEntity` member order defines it —
edit in place and the row is stored backwards, where the unique index stops
protecting it.

**Gate:** the promoted person keeps the deal, gains a `lifecycle_stage`, and
`check_invariants.py` still PASSes. Then **you commit.**

---

## Phase 8 — hygiene, as separate small commits

**Owner: me. No dependencies.** Each is independent; do them when convenient,
except 8.1.

| | Step | Why |
|---|---|---|
| 8.1 | **Per-account domain allowlist for the onsite embed**, and auth on `/onsite/forms-config` | ⚠️ **Before forms go live.** Today `company_id` is a raw user ObjectId in a public `<script>` URL and the config endpoint is unauthenticated — anyone can read any account's active forms and post to any known slug |
| 8.2 | `OPTIONS` handling for the three JSON onsite endpoints | `identify` / `track` / `form-view` are blocked by preflight on every real customer domain, so onsite analytics are dark in production |
| 8.3 | CORS headers on `/f/{slug}/submit` **error** responses | `HTTPException` carries none, so every real message becomes `alert('Network error.')` |
| 8.4 | Stamp `origin` on the four routes that do not | the public contact API, list-add, the async CSV importer, the nevtancloud tracker. ⚠️ **No backfill** — existing rows have no recoverable origin and guessing is worse than null |
| 8.5 | Read `body.fields` on the JSON signup submit, or remove it | declared and discarded with no log line (`content_builder.py:1600`) |
| 8.6 | `/onsite/identify`: store the other properties; stop being a no-op for existing contacts | `content_builder.py:2587-2594` |
| 8.7 | Quiz submit: write the `list_memberships` row | today it sets `contact.list_ids` only, so the lead is invisible in list views and triggers nothing |
| 8.8 | Let a returning lead correct their phone / name | written only if currently blank (`:1787-1792`) |
| 8.9 | Delete or document the dead code | `auto_link_service.py`, `crm_service_base_url`, the four `crm_client` comment references, `webhook_incoming_data` (written, consumed by nothing) |

**Gate:** each is its own small diff with its own check. **You commit each
separately** — these should not ride along with a feature phase.

---

## Dependency map

```
Phase 0  (you)  ─── gates everything
   │
   ├── Phase 1  docs surface            independent, do it now
   │
   ├── Phase 2  API key → CRM      🔸 backend?     ─┐
   │                                                ├── either delivers a deal
   ├── Phase 3  capture reaches a list               │
   │      └── Phase 4  lead vs subscriber  🔸        │
   │             └── Phase 5  door + caller  🔸     ─┤
   │                    ├── Phase 6  flow step  🔸  ─┘   needs an owner
   │                    └── Phase 7  promote
   │
   └── Phase 8  hygiene — 8.1 before forms go live
```

**Shortest path to a lead in a named pipeline:**
**Phase 0 → Phase 2.** One conditional, no flow engine, nobody's sign-off.

**If the site is static:** Phase 0 → 3 → 4 → 5 → 6, with phase 6 gated on the
`flow_executor.py` owner.

---

## The four answers that unblock the rest

| | Question | Blocks |
|---|---|---|
| 1 | Does the customer's website have a backend? | Phase 2 — and whether 5 and 6 are needed at all |
| 2 | Should an enquiry form skip double opt-in? | Phase 4 |
| 3 | Identity A/B/C, and company i/ii/iii | Phase 5 |
| 4 | Who owns `flow_executor.py`? | Phase 6 |

Plus the two small ones: is `reports` in `core` (phase 0), and can an Engage
customer buy API access (phase 2).

---

## Verification commands

The throwaway harnesses from earlier sessions lived in the device's `/tmp` and
were cleared overnight. What survives, and should be run at every gate:

```bash
# CRM_API
curl -s localhost:8100/health | jq           # 14 routers · 18 mounts · 95/95
python3 scripts/check_crm_contract.py        # PASS — contract matches routers
python3 scripts/check_invariants.py          # PASS — assoc order, precedence,
                                             #        32 permissions, cross-repo
python3 scripts/check_crm_contact_index.py   # report only, no --apply
python3 ~/h/filters.py                       # filter + saved-view endpoints

# emailMarketingUI
./node_modules/.bin/eslint --no-ignore src/screens/CRM/        # exit 0
TZ=Asia/Kolkata node --test src/screens/CRM/**/*.test.mjs      # 361 pass
```

⚠️ `TZ=Asia/Kolkata` is not optional — the task-helper suite refuses to run
under UTC on purpose, and reports that refusal as a failure.
