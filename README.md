# CRM_API

The Engage CRM, being extracted from `emailMarketingAPI`. The plan and its
reasoning are in [`EXTRACTION_PLAN.md`](EXTRACTION_PLAN.md).

**Status: Phase 6 — ready to cut over, and no longer tied to one host.** 12
route modules mounted, `endpoints 83/83`, contract checker PASS.

⚠️ **THERE ARE NO TESTS IN THIS REPO, AND THERE WERE 1,824.** 891 integration,
798 unit, 135 service, plus two mutation harnesses that killed all 32 of their
mutants. They were deleted deliberately; nothing in `app/` depends on them, so
the service imports, mounts its 83 endpoints and passes the contract check
without them.

What they were covering, in case it matters later: the tenancy rule
(`created_by == account_id`) and ten cross-tenant defects already filed
against it; the two-coercion-map bug behind `expected_close_date =
'cylonsign.com'`; the raw-`ObjectId`-in-an-untyped-`data` 500; the host
boundary's two failure classes, where confusing them creates duplicate
contacts during an import; and that `[CRM] profile` still defaults to `full`,
which is the only thing standing between a config edit and most of Engage's
CRM screens going 404. `scripts/crm_tests.py` holds the expected counts and
says what is missing if you run it; `.github/workflows/tests.yml` is trimmed
to the contract check, with the suite steps commented out ready to restore.

## Using this CRM from another project

**Pick how much of it you want.** One config line:

```ini
[CRM]
profile = core        ; full (default) | core | minimal
```

| profile | endpoints | modules | you get |
|---|---|---|---|
| `full` | 83 | 12 | everything Engage has — **the default, and it must stay the default** |
| `core` | 61 | 7 | companies, deals, pipelines + board, notes, tasks |
| `minimal` | 47 | 5 | records, pipelines + board |

`core` leaves out what Engage grew around the CRM: saved views, per-user
column layouts, the import wizard, export jobs, files with signed downloads.

⚠️ **One codebase, not a fork.** A profile trims the API surface, not the
source tree — so a `core` consumer has less to learn and secure, but the repo
is the same size. [`app/profiles.py`](app/profiles.py) carries the argument
against forking, and it is short: this project has already shipped two
coercion maps that disagreed about dates, two copies of the system-attribute
set, and one default written in four places. A forked CRM is that failure with
a two-year fuse, and the thing that drifts is a tenancy decision.

Then: the service needs two things from the app around it, and
[**`docs/host-contract.md`**](docs/host-contract.md) is the whole spec:

| | question | endpoint | required |
|---|---|---|---|
| **Identity** | who is this caller, and what may they do? | `GET /api/v1/internal/auth/context` | unless you use `jwt_claims` |
| **Directory** | what is this user / tag / contact called? | `POST /api/v1/internal/crm-host/directory` | no |

Both are pluggable (`app/host/`), configured in `[HOST]`, and
`emailMarketingAPI` is the reference implementation of both. The fastest way
to see it run is `identity = jwt_claims` + `directory = none`: no endpoints to
implement, and companies, deals, pipelines, the board, notes, tasks, files,
attributes, saved views, imports and exports all work — only owner names and
tag chips degrade.

⚠️ **The defaults are `introspect` + `shared_db`, which is the platform's
behaviour, and that is load-bearing rather than polite.** `emailMarketingAPI`
must get out of the host refactor exactly what it had going in, or the Phase 5
dual-run diff stops meaning anything. Most of `tests/test_host.py` defends
those defaults rather than the new adapters: the risk in that change was never
the new code, it was the old behaviour changing while nothing noticed. Two
mutation tests aimed at exactly that question survived the first attempt,
because the default was written out in four places and only one was
load-bearing.

The cutover is one nginx rule and the rollback is the same reload backwards —
[`docs/cutover.md`](docs/cutover.md) has the config, the nine-check gate
(`scripts/cutover_check.py`) and the watch (`scripts/watch_cutover.py`).

⚠️ **CORS is the trap nothing else catches.** The UI is served from a
different origin than the API, so every CRM request from a browser is
cross-origin — and neither `curl`, nor the test suite, nor the Phase 5 diff
enforces CORS. A service without `Access-Control-Allow-Origin` passes the
entire dual run and breaks every CRM screen the instant nginx is reloaded.
This service mirrors the platform's eleven-origin allow-list; `tests/test_cors.py`
pins it and the gate checks it against a running process.

`scripts/diff_against_platform.py` is the Phase 5 exit check: it asks both
services the same ~50 questions and compares every byte. **Read-only** — the
two share one database, so a compared write would happen twice.
[`docs/dual-run.md`](docs/dual-run.md) is the runbook.

A real token now reaches a CRM endpoint. Verified end to end against a running
server with a generated keypair:

| credential | answer |
|---|---|
| none, or a malformed token | `401 Invalid Credentials` |
| a password-reset token presented as a bearer | `401` — the same key signs both |
| a valid token, platform unreachable | `503`, never 401 |
| a valid token | the caller's account, role, grants and denials |

Nothing is routed to this service yet: nginx still sends `/api/v1/crm/` to the
platform. That is Phase 6.

**It needs one addition to the platform repo** —
[`docs/platform-internal-auth.py.txt`](docs/platform-internal-auth.py.txt) is
the file, the router registration and the nginx deny rule.

---

## Running it

```bash
cp config/app.conf.example config/app.conf     # then fill in [MAIN_DATABASE]
poetry install --no-root
poetry run uvicorn app.main:app --port 8100 --reload

python3 scripts/smoke.py                       # health + how much has landed
python3 scripts/crm_tests.py                   # all three suites, with baselines
python3 scripts/crm_tests.py --mutation        # + the two mutation harnesses
python3 scripts/check_crm_contract.py          # 83 endpoints vs the contract

# Phase 5, against a deployed pair (see docs/dual-run.md)
CRM_DIFF_TOKEN=<token> python3 scripts/diff_against_platform.py \
    --platform http://127.0.0.1:8000 --crm http://127.0.0.1:8100
```

The suites need **no database and no config** — the integration tests run
against a fake Mongo (`tests/integration/shims/`), which is why 891 of them
finish in three seconds.

`MAIN_DB_URI` and `MAIN_DB_NAME` in the environment override the file, exactly
as they do for the monolith — so pointing this at stage for one run needs no
file edit:

```bash
MAIN_DB_URI="$STAGE_URI" MAIN_DB_NAME=engage_stage \
  poetry run uvicorn app.main:app --port 8100
```

## The two health endpoints, and why there are two

| | touches Mongo | who watches it |
|---|---|---|
| `GET /health` | no | PM2, uptime checks |
| `GET /health/ready` | yes, 2s budget | nginx, the Phase 6 cutover |

Collapsing them into one endpoint that pings the database is the classic
mistake: Mongo blips, liveness fails, the supervisor kills a healthy process,
and the restart cannot fix the network. Readiness answers **503 with a reason**
— the missing config key by name, or a one-line database error — because a bad
deploy should say which value it wants, not leave you reading a connection
trace.

Readiness enforces its own 2-second budget with `wait_for`. Measured: with
`maxTimeMS` alone and a dead Mongo it took **30 seconds**, because PyMongo
spends its default server-selection budget before sending any command. A probe
slower than the thing polling it records a timeout and the body that would have
explained the fault never arrives.

## Decisions already made, and where they are written down

| Decision | Where | Short version |
|---|---|---|
| Tokens verified locally, identity asked of the platform | `app/auth/introspect.py` | RS256 needs no coordination; copying the platform's 800 lines of account/team/invite/key rules would have been the project's most expensive duplication |
| One database, two owners | `app/db.py` | This service owns 14 collections, reads 3 it does not own. No data migration |
| Config format unchanged | `app/config.py` | Same `app.conf`, same sections, same env overrides. The droplet's file already works |
| Log format unchanged | `app/config.py` | Byte-for-byte the monolith's, so every runbook and grep still works |
| No `tz_aware` on the Mongo client | `app/db.py` | The monolith omits it, so timestamps are naive. Changing it would alter every timestamp this service emits |
| `allow_index_dropping=False` | `app/db.py` | The monolith sets it True. Here it could drop the board's compound index during the dual-run |
| Library versions pinned *with* the monolith | `pyproject.toml` | Two services on one database must agree about serialisation while they are being diffed |
| Paths stay at `/api/v1/crm/...` | `app/main.py` | The cutover is an nginx rule, so the UI never learns there are two services |

## Nine dependencies

```
fastapi  pydantic  uvicorn  motor  beanie  PyJWT[crypto]  boto3
python-multipart  httpx
```

The monolith needs about 35. The CRM used almost none of them: stripe,
razorpay, openai, pymupdf, reportlab, pyhanko, pillow, jinja2, google-auth,
passlib, bcrypt, dnspython and aiohttp are all absent on purpose.

Before adding to that list, check whether the need is really a service-to-
service call. "The CRM must send an email" is not a reason to add an SMTP
library.

## Layout

```
app/
  config.py   configuration + the logger — all that `core_app` was to the CRM
  db.py       Mongo client, Beanie registration (MODELS is empty until Phase 2)
  main.py     FastAPI app, health, and the router mount point
config/       app.conf.example — the real file is gitignored
scripts/      smoke.py, and the CRM scripts in Phase 2
tests/        test_health.py; the 916 + 794 moved tests arrive in Phase 3
```

## What came over, and what deliberately did not

| | |
|---|---|
| 157 files, ~38,000 lines | `app/{models,crud,schemas,services,routes}`, the tests, the scripts |
| 7 copied primitives | `object_id.py`, `crud/_platform_base.py`, `crud/dependency.py`, `auth/rbac.py`, `auth/context.py`, `errors.py`, `enums.py` |
| `app/foreign.py` | the ONLY reads of `users`, `tags`, `contacts` — the three collections this service does not own |
| **not** `test_p2_contact_scope.py` | 25 tests of *platform* code; see `tests/integration/MOVED_OUT.md` |
| **not** the task-reminder scheduler, yet | it moved as code but is not started — the monolith is still running its copy. Phase 7 |

`app/services/contact_scope.py` is duplicated with the monolith on purpose for
now; `MOVED_OUT.md` explains when that ends.

## Getting the history

`scripts/import_history.sh` imports the CRM's commits from the monolith with
`git filter-repo`, then lays the rewritten tree over them in a second commit —
so `git log --follow app/routes/deals.py` walks back into
`api/routes/crm/deals.py`. Run it after the Phase 1 commit; it commits nothing
itself.

That history is most of the value of 38,000 lines: the comments explain which
bug each guard stands in front of, and `git blame` is how the next person
finds the ones the comments do not name.

## The suites, and the one thing they cannot see

| suite | tests | what it is |
|---|---|---|
| `tests/integration` | 891 | the real route handlers, against a fake Mongo. No database, three seconds |
| `tests/unit` | 798 | the pure rule modules — stage rules, filters, attributes, serializers |
| `tests/test_health.py` | 11 | the service's own lifecycle: liveness, readiness, a failed registration |
| `tests/test_suite_integrity.py` | 6 | does this suite test THIS repo, and is the shim trap still the shim trap |
| `tests/test_auth.py` | 29 | token verification and context resolution, offline with a generated keypair |
| `tests/test_diff.py` | 22 | the cross-service comparison itself — too strict and it cries wolf, too loose and it signs off on a divergence |
| `tests/test_cors.py` | 11 | the allow-list and a browser-shaped preflight. The one cutover failure no other check can see |
| `tests/mutation/` | 32 mutants | plausible wrong implementations the suites must kill |

⚠️ **The fake `ObjectId` is a `str` subclass.** That is what makes the
integration suite work without a database, and it is also the one thing it
cannot see: a raw `bson.ObjectId` leaking into an untyped response is a
`PydanticSerializationError` — a 500 — and `isinstance(x, str)` is True either
way. It took out `GET /crm/deals/{id}/companies` in production while its tests
were green.

So wire-type assertions read `self.assertIs(type(value), str)`, because
`json_safe` normalises a str subclass to exactly `str` and the shim's
ObjectId is not. `test_suite_integrity.py` fails if a weak `isinstance` one
appears, and pins the premise so that if the shim ever becomes a real
ObjectId, the suite says the discipline can be relaxed.

## How authentication works

Two steps, deliberately split:

1. **The token is verified here.** `app/auth/tokens.py`, RS256 against the
   public key — the asymmetric half is the whole reason this needs no
   coordination with the platform. A forged, expired, malformed or
   wrong-type token is rejected with no network call, which is the volume an
   attacker controls. The `type == "access_token"` check is carried over
   deliberately: the platform signs password-reset tokens with the same key.

2. **Who the caller is comes from the platform.** `app/auth/introspect.py`
   calls `GET /api/v1/internal/auth/context`, cached 30 seconds against a
   hash of the credential.

⚠️ **Step 2 is not what `EXTRACTION_PLAN.md` §4.2 said**, and the reversal is
documented at length in `introspect.py`. The plan costed "copy `context.py` +
`rbac.py`" at 461 lines; the transitive closure is nearer 800 — account,
team, invite and API-key rules, none of it CRM. The plan's own §8 lists
"membership logic drifts after being copied" as a risk, and that would have
been it. The platform endpoint declares the *existing* `get_request_context`
dependency, so there is no second implementation anywhere, and API keys work
on day one.

`rbac.py` **is** copied — it is pure logic, and `require()` runs on all 83
endpoints, so no round trip may stand between a request and a permission
check. Role, grants and denials arrive once per 30 seconds; every `ctx.has()`
after that is local.

The failure modes all refuse rather than defaulting. There is no path through
`get_request_context` that produces a context with a guessed account id,
because every CRM query is scoped by that value —
`test_no_failure_mode_produces_a_context` pins all seven.
