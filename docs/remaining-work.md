# What is left on our side

Status as of **28 Oct 2026**, measured rather than remembered. Written so it
can be turned into phases and tickets directly.

Four groups, and they are deliberately in this order:

| | | Blocks what |
|---|---|---|
| **A** | Blocked on you — no code from me | everything below |
| **B** | In flight, finished, uncommitted | the lead work builds on it |
| **C** | The lead → pipeline path (planned, not started) | the actual feature |
| **D** | Defects found while investigating, not fixed | nothing, individually |

---

## A. Blocked on you — nothing I can do from here

| | What | Why it is yours | Cost |
|---|---|---|---|
| **A1** | `python3 scripts/check_crm_contact_index.py --apply`, then restart | Needs your database. The stale `uniq_crm_contact_email` was built `sparse=True`; the model now declares it partial, and `allow_index_dropping=False` is deliberate so Beanie will not resolve it | one command |
| **A2** | **Commit and push** group B | Your standing rule: *"do not commit any code i will do myself"* | — |
| **A3** | Rotate the `internal_key` and the `admin:admin` `db_uri` | Both were exposed in this session's transcript — the key by a failed redaction of mine | — |
| **A4** | Browser pass on **Reports** and the restored **filter bar / saved views** | The pane redirected to `/login` and signing in as you is not something I will do. ⚠️ `no-undef` is **off** in this eslint config, so lint cannot prove a JSX element resolves — the `Avatar is not defined` class of bug. I checked all 63 elements by hand, which is a good substitute and not a complete one | 10 min |
| **A5** | Decide: does `reports` belong in the `core` / `minimal` profile? | Currently `full` only, with the reasoning recorded in `app/profiles.py:76` — a fresh consumer has no history to analyse | one line |
| **A6** | Answer the six questions in `lead-intake-plan.md` §8 | **A6.1 decides how much of group C is relevant at all** | — |

⚠️ **A1 is the real gate.** `POST /crm/contacts` still 500s on your box until
it runs, so nothing in the contact → company → deal flow is testable end to
end — including everything in group C.

---

## B. In flight and finished, waiting only on a commit

**Correction to what I have been repeating:** the contacts restore **is
committed** in both repos — `CRM_API` at `a6e46fb`, `emailMarketingUI` at
`47a1596`, and `app/models/contact.py`, `app/routes/contacts.py`,
`scripts/check_invariants.py` and `check_crm_contact_index.py` are all tracked
and clean. Only this session's work is outstanding. I had been saying "phases
1–4 are uncommitted"; that was wrong.

`emailMarketingAPI` has **no uncommitted changes** — I have not touched it.

### CRM_API — 4 modified, 5 new

```
 M app/main.py                              EXPECTED_ROUTERS 14, ENDPOINTS 95, reports mount
 M app/profiles.py                          reports in ALL_MODULES + full
 M docs/api-contract.md                     regenerated with --emit
 M scripts/migrate_crm_contacts_removal.py  the refuse-if-contacts-are-back guards
?? app/routes/reports.py                    4 endpoints
?? app/services/reporting.py                the aggregations
?? docs/lead-intake-plan.md                 group C's plan
?? docs/payaca-phases.md  docs/payaca-gap-analysis.md
```

### emailMarketingUI — 10 modified, 1 new directory

```
 M src/Utility/crm.js  src/constant/Environment.jsx     4 report endpoints + helpers
 M src/routes/routeMap.js  src/screens/index.js
   src/constants/features.js  src/constants/menuConfig.js  the /crm/reports route
 M src/screens/CRM/Companies/index.jsx                   filter bar + saved views un-hidden
 M src/screens/CRM/Deals/index.jsx                       same, list view only
 M src/screens/CRM/Companies/CompanyDetails.jsx          + New deal, Marketing contacts removed
 M src/screens/CRM/Contacts/ContactDetails.jsx           lifecycle Select
?? src/screens/CRM/Reports/                              7 components + helpers + 16 tests
```

Verified: `/health` → `14 routers · 18 mounts · 95/95`, contract **PASS**,
invariants **PASS**, `eslint --no-ignore src/screens/CRM/` **exit 0** (was 12
errors), 361 UI helper tests pass (`TZ=Asia/Kolkata`), and a harness confirms
every field the Reports screen reads exists in the four responses.

**Nothing in group B needs more work from me.** It needs A2 and A4.

---

## C. The lead → pipeline path — planned, nothing started

Full detail in `docs/lead-intake-plan.md`. Summarised here because the shape
depends entirely on **A6.1: does the customer's website have a backend?**

### Track 1 — their backend posts directly (plan §2.3)

| | What | Where | Size |
|---|---|---|---|
| **C1** | Let an API key authenticate to the CRM — skip `tokens.verify` when the bearer starts with `eng_live_`, let introspection answer | `CRM_API/app/auth/context.py:127-135` | **one conditional** + tests |
| **C2** | Decide the API-key packaging — keys are gated behind `require_api_keys_enabled` or a Workspace plan with `engage-api-access` | commercial | — |
| **C3** | Document the two-call shape (company by name → deal with `pipeline_id` + `stage_id`) | docs | small |

⚠️ **C1 replaces C4–C9 entirely** for a customer who runs their own site. It
is testable with curl on day one, needs nobody's sign-off, and everything
downstream is already built for it — the platform's introspection returns
`is_api_key`, the CRM already carries the marker, and a key's scopes arrive as
`extra_grants` which `has_permission` already unions in. **Do this first
regardless**, because it is cheap and it de-risks the rest.

### Track 2 — marketing-driven intake (plan §6)

| | What | Repo | Size | Blocked by |
|---|---|---|---|---|
| **C4** | Phase 0a — honour `double_opt_in`; split *lead* from *subscriber* | platform | medium | **A6.2** (compliance call) |
| **C5** | Phase 0b — refuse to publish a form/page with no list, server-side | platform + UI | small | |
| **C6** | Phase 0c — fire the trigger on opt-in confirm, via `ListMembershipCrud` | platform | small | |
| **C7** | Phase 0d — somewhere for a company name to go (§4.1 i/ii/iii) | both | small→medium | **A6.3** |
| **C8** | Phase 1 + 2 — the CRM's internal door, and the platform's caller | both | medium | |
| **C9** | Phase 3 — the `crm_deal` flow step + builder UI | platform + UI | medium | **A6.4** — who owns `flow_executor.py` |
| **C10** | Phase 4 — promote a lead to a `crm_contact` | both | small | C8 |
| **C11** | Phase 5 — the follow-up task, and the 4 missing `origin` stamps | both | small | |

⚠️ **C9 is the only item in this document that needs somebody else's
agreement.** It is a *smaller* ask than CRM-417 — additive code, about a
contact, keyed on `(flow_id, contact_id)` which is exactly the key CRM-417 §3.2
says cannot express a deal event. Worth asking as its own question rather than
bundling it with the stalled one.

---

## D. Found while investigating, not fixed

None of these block group C. Each is independent, and the first two are the
ones I would not leave.

### D1 — ⚠️ `/docs`, `/redoc` and `/openapi.json` are served **in production**

`api/main.py:410-424` computes `_docs_on` correctly and then has all three
arguments **commented out**, so FastAPI falls back to its defaults.
`api/maincon.py:358-372` has the gate applied properly — but `dev_api.py:12`
and the process manager boot `api.main:app`, the ungated one. The CRM service
has no docs settings at all, so its `/docs` is open too.

**Three commented lines and the whole endpoint surface is public.** Smallest
fix in this document and the highest ratio of risk to effort.

### D2 — ⚠️ the onsite embed has no real account binding

The snippet's only account identity is `company_id=<raw user ObjectId>` in a
public `<script>` URL (`content_builder.py:2405`), and
`GET /onsite/forms-config?company_id=…` is **unauthenticated** — so anyone can
read any account's active forms, and post to any known slug. There is **no
per-account domain allowlist** anywhere in either repo.

Pre-existing, not introduced by anything here. **Putting Landing Pages and
Signup Forms into production is the moment it starts mattering**, which is why
it is in this list and not just a note.

### D3 — silent data loss on the intake paths

| | What | Where |
|---|---|---|
| a | `double_opt_in` is stored, shown in the editor, and **read by nothing** — DOI is unconditional. `compliance.requires_double_opt_in` is written *and tested* with **no caller** | `signup_form.py:49`, `compliance.py:206` |
| b | A list-less form/page captures leads that trigger nothing, **ever**, and says "Thanks for subscribing!" — `POST /signup-forms/{id}/status` has no list check | `content_builder.py:1247-1261` |
| c | `POST /signup-forms/public/{id}/submit` declares a `fields` dict and **discards it**, with no log line. Also unused by the UI | `content_builder.py:1600` |
| d | `/onsite/identify` ignores every property but `first_name`, and is a **complete no-op for an existing contact** | `content_builder.py:2587-2594` |
| e | Quiz submit sets `contact.list_ids` but writes **no `list_memberships` row** → invisible in list views, triggers no flow | `content_builder.py:2791` |
| f | On an existing contact, `first_name`/`last_name`/`phone` are written **only if blank** — a lead correcting their phone number is ignored | `content_builder.py:1787-1792` |
| g | 4 of 12 intake routes never stamp `origin` — the public contact API, list-add, the async CSV importer, the nevtancloud tracker | §2.3 of the plan |

### D4 — CORS edges

| | What |
|---|---|
| a | The three JSON onsite endpoints (`identify`, `track`, `form-view`) are blocked by preflight on every real customer domain — `CORSMiddleware` answers `OPTIONS` before the handler's `ACAO: *` can run. Capture works only because `forms.js` posts multipart |
| b | Error responses from `/f/{slug}/submit` are `HTTPException` and carry **no** CORS header, so the browser blocks the body and the visitor gets `alert('Network error.')` instead of the real message |

### D5 — dead code and stale references

| | What |
|---|---|
| a | A **truncated comment** at `CRM_API/app/main.py:479` for the router phase 4a deleted — the sentence stops mid-clause. Either restore the router (C8) or finish the comment; right now it reads like a bug |
| b | `app/services/auto_link_service.py` — intact, no caller since phase 4a |
| c | `crm_service_base_url` + `internal_api_key` parsed at `app/__init__.py:554-560`, **read by nothing**; four files still reference the deleted `api/services/crm_client.py` in comments |
| d | `webhook_incoming_data` is written by `/webhook/import` and **consumed by nothing** — no worker, no scheduler. The inbound token is not shown anywhere in the UI |
| e | `CrmRecordSource.AUTOMATION` exists and has never been written (C8 is what writes it) |
| f | 8 permissions no built-in role grants — `invariants` reports it as a note, not a failure. An endpoint gated on one is unreachable by default |

---

## Suggested order

| | | Why here |
|---|---|---|
| 1 | **A1 → A2 → A4** | the gate. Nothing is testable before A1, and group B should not sit uncommitted while more is built on it |
| 2 | **D1** | three commented lines, public endpoint surface |
| 3 | **A6.1**, then **C1 + C3** | if their site has a backend, one conditional delivers a lead into a named pipeline and stage — and makes track 2 optional |
| 4 | **C5, C6** then **C4, C7** | the cheap capture fixes first; C4 waits on the compliance answer |
| 5 | **C8**, then **C9** once A6.4 is answered | the marketing track, in dependency order |
| 6 | **D2** before forms go live; **D3, D4, D5** as they come up | |

**The honest summary:** group B is finished and needs a commit; group A is
yours and A1 gates everything; group C is one conditional away from working
for backend-driven leads and a multi-phase project for marketing-driven ones;
group D is a list of things that are wrong but not in the way.
