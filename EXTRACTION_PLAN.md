# Extracting the CRM into `CRM_API`

A plan to move the CRM out of `emailMarketingAPI` and into a small Python
service of its own, with the monolith and the UI still working at every step.

Written 25 Oct 2026. Every number below was measured against the working tree,
not estimated — the commands are in §9 so you can re-measure after any change.

---

## 1. What is actually being moved

| Tree | Files | Lines |
|---|---|---|
| `api/routes/crm/` | 13 | 5,614 |
| `api/services/engage/crm/` | 33 | 9,166 |
| `app/db/crud/main/crm/` | 15 | 1,385 |
| `app/db/models/main/crm/` | 15 | 1,168 |
| `api/schemas/main/crm/` | 13 | 1,570 |
| `tests/crm_integration/` | 41 | 11,820 |
| `tests/test_engage_crm_*.py` | 19 | 5,526 |
| `scripts/*crm*.py` | 8 | 1,690 |
| `app/enums/crm.py` | 1 | 142 |
| **Total** | **158** | **38,081** |

83 endpoints across 12 routers. 916 integration tests, 794 unit tests, plus a
contract checker that fails on drift between the routers and the documented
surface.

The monolith is ~160,000 lines outside its tests, so this is roughly a fifth
of the codebase leaving — and the part with the clearest ownership.

---

## 2. The finding that makes this cheap

**The CRM imports seven things from the rest of the monolith. That is all.**

```
44 sites  app.db.models.mongodb_object_id   PyObjectId — 20 lines, pure
85 sites  app (core_app)                    ONLY ever core_app.logger
12 sites  api.dependencies.context          RequestContext, require() — the auth seam
12 sites  api.dependencies.crud             get_crud() — 25 lines
 1 site   app.db.models.main.contacts       the platform Contact (import_worker)
 1 site   api.services.contact_scope        tenancy helpers, already standalone
 1 site   app.db.crud.main.base             CrudBase
```

Of the 85 `core_app` references, **every one is `core_app.logger`.** No config
reads, no app state, no service locator. That is the difference between a
service extraction and a rewrite.

Six of the seven are small and self-contained enough to copy outright. The
seventh — `api.dependencies.context` — is the only real design decision, and
§4 handles it.

---

## 3. The coupling that runs the *other* way, which is the real work

Three places in the monolith reach into the CRM. These do **not** move; they
become the service's public edge.

### 3.1 Auto-link: the only synchronous cross-boundary write

`api/routes/contacts.py` (contact create) and `api/routes/lists.py` (list
import) both call `auto_link_service` to create or link a CRM company from a
contact's email domain.

The existing call site already says what to do about it:

> *"After the ids are resolved and outside the membership write, so a failure
> here cannot cost the import."*

It is already wrapped in `try/except` and already best-effort. **So turning it
into an HTTP call does not change its failure semantics** — which is exactly
why this is the right seam and not a scary one. `POST /internal/crm/auto-link`,
short timeout, failure logged and swallowed, same as today.

One behaviour to preserve deliberately: the list importer builds **one**
`AutoLinker` for the whole batch, because "five thousand contacts at the same
company must be one company lookup, not five thousand — and must not race
itself into creating the company twice." The internal endpoint therefore takes
a **batch** of contacts, not one per call. Getting this wrong replaces one
lookup with five thousand HTTP requests.

### 3.2 The task-reminder scheduler

`api/main.py` starts `TaskReminderScheduler` on boot. It is self-contained
(reads `crm_tasks`, claims each reminder with a conditional update on
`reminder.sent_at`, so it is already safe under multiple workers). It moves
into the new service wholesale and is deleted from `api/main.py`.

### 3.3 The Beanie model registry

`app/db/models/registry.py` registers all 14 CRM models. The monolith must keep
these **until 3.1 is an HTTP call**, because `contacts.py` and `lists.py`
instantiate `CompanyCrud` / `CrmAssociationCrud` directly. They come out in the
same step, not before.

---

## 4. Three decisions to make before any code moves

### 4.1 One database, or two?

**Recommendation: one database, two owners.**

The CRM owns 14 collections: `companies`, `deals`, `pipelines`,
`crm_activities`, `crm_associations`, `crm_attributes`, `crm_exports`,
`crm_files`, `crm_imports`, `crm_notes`, `crm_saved_views`, `crm_settings`,
`crm_tasks`, `crm_view_preferences`.

It **reads** exactly three it does not own — `users` (owner expansion), `tags`
(company tag chips), `contacts` (the company Contacts tab, and resolving a
note's or task's target to a platform contact). It writes to none of them.

That is already a clean ownership split. Keeping one database buys:

* **no data migration** — the collections stay where they are;
* tenancy unchanged (`created_by == account_id`, enforced by
  `queries.assert_account_scoped`);
* the three foreign reads keep working as reads.

Put those three reads behind one module (`app/foreign.py`) so that if you ever
do split the database, there is a single file to turn into HTTP calls. Do not
scatter `db["contacts"]` through the new service.

The cost: two processes write one database, so a schema change to a CRM
collection must be deployed in step. Since only the CRM writes CRM
collections, that is a change to one service.

> ⚠️ `companies`, `deals` and `pipelines` are **unprefixed** collection names in
> a shared database. Do not rename them (every document would need rewriting
> and `scripts/repair_crm_typed_columns.py` already shows what a wrong
> collection name costs — it silently reports a clean database). Record the
> names as reserved somewhere the platform team reads.

### 4.2 How the service authenticates a request

The tokens are **RS256** — asymmetric. The service verifies them with the
public key alone; no call home, no shared secret.

What it cannot do locally is resolve *membership*: which account, which role,
which custom grants, which denials. `api/dependencies/context.py`
(283 lines) + `api/services/engage/rbac.py` (178 lines, pure) do that from the
`users`, team-member and custom-role collections.

**Recommendation: copy both, read those collections read-only.** `rbac.py` is
pure logic and moves unchanged. It keeps RBAC decisions local — no per-request
round trip — which matters because `require()` runs on all 83 endpoints.

The alternative, having the monolith mint an internal token carrying role and
grants, is better long-term and worse now: it needs a change on both sides and
a new token type to get wrong. Revisit when the databases split.

Whichever you choose, the module-level permission constants (`WRITE`, `READ`)
must keep their current names — `scripts/check_crm_contract.py` resolves them
to verify the documented permission on every endpoint.

### 4.3 How the UI reaches it

The UI has a single base URL (`NEXT_PUBLIC_API_BASE_URL`) and 83 CRM endpoints
behind `${API}/crm/...`.

**Recommendation: route at the edge, change nothing in the UI.** nginx on the
droplet sends `/api/v1/crm/` to the new process and everything else to the
monolith. Benefits: the UI ships unchanged, the cutover is one nginx reload,
and the rollback is the same reload backwards.

For local development, a Next rewrite in `next.config.js` does the same job so
nobody has to run a proxy.

Avoid a second base URL in the UI unless the edge route is impossible: it
would touch `Environment.jsx`, every CRM call, and the preview deploys, and it
makes "which API am I talking to" a client-side question.

---

## 5. The new service's shape

```
CRM_API/
├── pyproject.toml           ~9 dependencies (§6)
├── ecosystem.config.js      a second PM2 app, mirroring the monolith's
├── README.md                how to run it, and the three decisions above
├── app/
│   ├── main.py              FastAPI app, 12 routers, lifespan = Beanie init
│   ├── config.py            config/app.conf reader + the logger (replaces core_app)
│   ├── db.py                motor client, Beanie init for the 14 models
│   ├── foreign.py           the ONLY reads of users / tags / contacts
│   ├── auth/
│   │   ├── context.py       RequestContext, require()  (copied, §4.2)
│   │   └── rbac.py          pure permission logic      (copied unchanged)
│   ├── models/              ← app/db/models/main/crm/      (15)
│   ├── crud/                ← app/db/crud/main/crm/ + base (16)
│   ├── schemas/             ← api/schemas/main/crm/        (13)
│   ├── services/            ← api/services/engage/crm/     (33)
│   ├── routes/              ← api/routes/crm/              (13)
│   └── enums.py             ← app/enums/crm.py
├── scripts/                 ← the 8 CRM scripts, paths adjusted
├── tests/
│   ├── integration/         ← tests/crm_integration/  (41, incl. the shims)
│   └── unit/                ← tests/test_engage_crm_*  (19)
└── docs/
    └── api-contract.md      ← moved out of the UI repo (§7.6)
```

Flattening `api/services/engage/crm/` to `app/services/` means ~46 import
rewrites, all mechanical. Keep module basenames identical
(`stage_rules.py`, `attribute_routing.py`, …) so every cross-reference in the
comments — and there are a lot of them, naming specific modules and specific
bugs — still points at a real file.

---

## 6. The dependency surface, which is the point of a small service

The monolith needs ~35 packages. The CRM needs nine:

```
fastapi  uvicorn  pydantic  motor  beanie
PyJWT[crypto]            # RS256 verification
boto3                    # crm_files storage (lazily imported today)
python-multipart         # file upload
httpx                    # calling the monolith, if ever needed
```

Not needed, and worth being explicit about so they do not creep back:
stripe, razorpay, openai, pymupdf, reportlab, pyhanko, pillow, jinja2,
google-auth, passlib, bcrypt, dnspython, aiohttp.

That is the measurable win: a service that installs in a fraction of the time,
has a fraction of the CVE surface, and restarts without touching billing,
email or PDF code.

---

## 7. Sequence

Each phase ends with something verifiable. Do not start a phase until the
previous one's exit test passes.

### Phase 0 — land what is in flight (½ day)

Both working trees currently hold uncommitted work (the typed-column fix, the
deal↔company endpoint, the date-kind split, the avatar/capitalise pass).
**Commit and push all of it first.** A copy made mid-change loses whatever is
not on disk in the state you think it is.

Then freeze: no new CRM features land in the monolith while the move is in
flight. One or two days, not weeks — which is why the phases below are small.

*Exit:* `scripts/crm_tests.py` and `npm run crm:test` green, both trees clean.

### Phase 1 — skeleton (1 day)

`pyproject.toml`, `app/main.py` with an empty router set, `app/config.py`,
`app/db.py`, a health endpoint. Boots, connects to Mongo, initialises Beanie
with zero models.

*Exit:* `GET /health` answers against the real stage database.

### Phase 2 — move the code, with its history (1–2 days)

Use `git filter-repo` (or `git subtree split`) to produce a branch containing
only the CRM paths, then pull that into `CRM_API`. **Do not copy the files
with `cp`.**

The comments in this codebase explain why each guard exists — the str-Enum
trap, the if/else-on-object-type bug class, the naive-UTC timestamps, the
pydantic-does-not-validate-on-assignment write path. `git log` and `git blame`
are how the next person finds out which bug a line is standing in front of.
Losing that history is losing most of the value of 38,000 lines.

Then: the seven copied primitives (§2), the import rewrites, `core_app.logger`
→ `app.config.logger`.

*Exit:* `python -c "import app.main"` succeeds; `GET /openapi.json` lists 83
endpoints across 12 routers.

### Phase 3 — tests green in the new repo (1 day)

**This is the phase that de-risks everything else, and it is nearly free.**

`tests/crm_integration/` runs against a fake Mongo — `harness.py` binds a stub
`AsyncIOMotorDatabase` and `shims/` fakes `beanie`, `motor`, `pymongo` and
`bson`. 916 tests, no database, no network. They move as-is and prove the
extracted code behaves identically.

Port `scripts/crm_tests.py` with its baselines (916 / 794) and
`scripts/check_crm_contract.py`.

> ⚠️ Carry one piece of discipline across with them. The shims' `ObjectId`
> **subclasses `str`**, so a raw `bson.ObjectId` leaking into an untyped
> response is invisible to this suite — which is how
> `GET /deals/{id}/companies` passed its tests and answered 500 in production.
> Assertions on wire types must read `type(x) is str`, never `isinstance`.

*Exit:* 916 + 794 green in `CRM_API`; contract checker PASS.

### Phase 4 — auth (1 day)

Copy `context.py` + `rbac.py` per §4.2, point them at the public key and the
membership collections. The RBAC matrix test (`test_e11_rbac_matrix.py`) is the
proof.

*Exit:* a real stage token reaches a CRM endpoint and gets the same answer the
monolith gives for the same token; a token without `deals:read` is refused.

### Phase 5 — dual-run (1–2 days)

Deploy as a second PM2 app on the stage droplet, port it to itself, nginx route
**not** switched. Both services now serve the CRM; the UI still talks to the
monolith.

Compare them: for a set of real accounts, call the same endpoints on both and
diff the JSON. Any difference is a porting bug, and this is the cheapest place
to find one.

Both will create indexes on boot (Beanie does it at init). That is idempotent,
but expect the log lines.

*Exit:* a scripted diff over the read endpoints of ≥3 accounts is empty.

### Phase 6 — cut over (½ day)

nginx: `/api/v1/crm/` → the new service. Watch for an hour. Rollback is the
previous nginx config.

*Exit:* CRM screens work end to end; no 5xx on the CRM path.

### Phase 7 — remove the CRM from the monolith (1 day)

In one commit, in this order:

1. `POST /internal/crm/auto-link` in the new service (batch, §3.1);
2. `contacts.py` and `lists.py` call it over HTTP, keeping the existing
   try/except and adding a timeout;
3. delete the router registrations in `routes_handler.py`;
4. delete `TaskReminderScheduler` from `api/main.py`;
5. delete the 14 CRM model registrations from `registry.py`;
6. delete the moved trees;
7. drop the now-unused dependencies if nothing else uses them.

*Exit:* the monolith boots, its own suite is green, creating a contact still
creates and links the company (the test for that now crosses a network
boundary and must be written as such).

### Phase 8 — tidy (½ day)

`docs/CRM/api-contract.md` moves from the **UI** repo to `CRM_API/docs/`
(it documents an API, and the checker that verifies it will live here). Update
`check_crm_contract.py`'s path resolution. Move `ENGAGE.md`'s CRM sections.
Point CI at the new repo.

**Total: 8–11 working days** for one person who knows this code, assuming no
surprises in Phase 5. Phases 0–4 are reversible at any point — nothing in
production changes until Phase 6.

---

## 8. Risks, with what to do about each

| Risk | Why it bites | Mitigation |
|---|---|---|
| A porting bug the shimmed suite cannot see | The fake `ObjectId` is a `str` subclass; serialization faults are invisible | Phase 5's live diff is the real test. Assert `type(x) is str` on wire payloads |
| Auto-link turned into one HTTP call per contact | A 5,000-row list import becomes 5,000 requests | The internal endpoint takes a batch; load-test with a 5,000-row import before Phase 7 |
| Two services creating indexes | Beanie does it at init in both during dual-run | Idempotent; just know it happens. After Phase 7 only the CRM service does |
| Membership logic drifts after being copied | Two copies of RBAC, as always | `rbac.py` is pure — copy it unchanged and keep the RBAC matrix test in both repos until the databases split |
| A platform collection collides with `companies` / `deals` / `pipelines` | They are unprefixed in a shared database | Reserve the names in the platform repo's docs now |
| Freeze overruns and the trees diverge | 38,000 lines re-merged by hand | Keep the freeze to days; `git filter-repo` makes a re-run of Phase 2 cheap if needed |
| The CRM needs a *new* monolith capability later (email, billing) | It deliberately has none of those dependencies | That is a service-to-service call, not a dependency. Decide it when it happens, not now |

---

## 9. Re-measuring any of this

```bash
# the trees and their sizes
for d in api/routes/crm api/services/engage/crm app/db/crud/main/crm \
         app/db/models/main/crm api/schemas/main/crm tests/crm_integration; do
  printf '%-34s %3s files %7s lines\n' "$d" \
    "$(find $d -name '*.py' | wc -l)" \
    "$(find $d -name '*.py' -exec cat {} + | wc -l)"
done

# what the CRM imports from outside itself  (§2 — re-run after every move)
grep -rhoE '^\s*(from|import)\s+(app|api)[\w.]*' \
  api/routes/crm api/services/engage/crm app/db/crud/main/crm \
  app/db/models/main/crm api/schemas/main/crm \
  | grep -v crm | sort | uniq -c | sort -rn

# what reaches INTO the CRM  (§3 — this list must reach zero by Phase 7)
grep -rn '^\s*\(from\|import\).*crm' --include=*.py api/ app/ \
  | grep -v '^api/routes/crm/\|^api/services/engage/crm/\|^app/db/crud/main/crm/\|^app/db/models/main/crm/\|^api/schemas/main/crm/'

# the collections the CRM owns, and the ones it only reads
grep -rh 'name = "' app/db/models/main/crm/*.py | tr -d ' "' | cut -d= -f2 | sort
grep -rho 'db_session\["[a-z_]*"\]' api/routes/crm api/services/engage/crm \
  app/db/crud/main/crm | sort -u
```

---

## 10. What this plan does not do

* **No database split.** Three read dependencies on `users`, `tags` and
  `contacts` stay reads against the same database, behind one module. Splitting
  is a separate project with real data-migration work, and nothing here blocks
  it later.
* **No rewrite.** The code moves as it is, including every comment explaining a
  bug it is standing in front of. Fixing things while moving them makes a
  failed Phase 5 diff impossible to attribute.
* **No new abstractions** between the monolith and the CRM. One internal
  endpoint, one nginx rule. A message bus is a reasonable later step and a bad
  first one.
* **No UI change.** The 83 endpoint paths do not move, so the UI never learns
  there are two services.
