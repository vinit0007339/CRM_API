# Phase 6 — the cutover

One nginx rule. The rollback is the same reload backwards.

**Do not start until** `scripts/diff_against_platform.py` is clean on three
accounts (Phase 5) **and** `scripts/cutover_check.py` passes.

---

## 1. The gate

```bash
export CRM_DIFF_TOKEN="<a real access token>"
python3 scripts/cutover_check.py \
    --crm        http://127.0.0.1:8100 \
    --host       https://<public api host> \
    --ui-origin  https://stage-engage.nevtan.com
```

Nine checks, each one a thing that has already bitten this project or would be
found by a user within a minute of the reload:

| check | what it catches |
|---|---|
| crm is live | the process is not running |
| every router is mounted | 11/12 modules — a 404 on one screen |
| crm is ready | the database is unreachable, or Beanie did not register |
| auth is configured | missing key or platform URL → 503 on every request |
| auth refuses a bad token | a **503** here means the platform's `/internal/auth/context` is unreachable: real requests would 503 too. A **401** means the chain works |
| auth accepts a real token | the only check that proves a user can get in |
| cors answers the UI's origin | **the one the diff tool cannot make** — see below |
| internal auth is not public | the Phase 4 endpoint reachable from the internet |
| public /crm is answering | confirms which service nginx is on, before and after |

### ⚠️ CORS is the trap, and it is invisible to everything else

The UI is served from a different origin than the API
(`https://stage-engage.nevtan.com` → the API host), so **every CRM request
from a browser is cross-origin**, and the platform answers them with an
eleven-origin allow-list in `api/main.py`.

The moment nginx routes `/api/v1/crm/` to the CRM service, that service owns
those headers. `curl` does not enforce CORS. Neither does
`diff_against_platform.py`. So a service with no `Access-Control-Allow-Origin`
passes the entire dual run, every test, and every manual curl — and then
breaks every CRM screen the instant the rule is reloaded.

This service sends them (`app/main.py`, pinned by `tests/test_cors.py`, 11
tests). The gate checks it against a running process, which is the only place
the answer is real. **If the platform ever changes its allow-list, change
`[CRM] cors_origins` with it.**

## 2. The rule

```nginx
# ── /api/v1/internal/ — the platform only, and never from outside ──────────
# From Phase 4. Keep it ABOVE the CRM block: both are `^~`, nginx picks the
# longest matching prefix, and this one must never be routed anywhere else.
location ^~ /api/v1/internal/ {
    allow 127.0.0.1;
    allow ::1;
    deny  all;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# ── /api/v1/crm → the CRM service ──────────────────────────────────────────
# `^~` and NO trailing slash on the prefix, deliberately:
#   · `^~` stops any regex location from winning over this one;
#   · without the trailing slash it also matches a bare `/api/v1/crm`, which
#     a client or a redirect can produce. With `/api/v1/crm/` only, that one
#     request would fall through to the platform — which still has the
#     routes today and so would answer it, hiding the gap until Phase 7
#     deletes them and it becomes a 404.
# `proxy_pass` with no URI part preserves the full path, so /api/v1/crm/deals
# arrives as /api/v1/crm/deals. Do not add a trailing slash to the proxy_pass
# target: that would rewrite the path and every endpoint would 404.
location ^~ /api/v1/crm {
    proxy_pass http://127.0.0.1:8100;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Authorization and X-Account-Id pass through untouched — nginx forwards
    # request headers by default, and the CRM needs both.

    # ⚠️ SET THIS HERE EVEN IF THE PLATFORM BLOCK ALREADY HAS IT. A
    # `client_max_body_size` inside `location /api/` does NOT apply to a
    # sibling location: directives inherit from http/server level, not from
    # another location. Miss it and file upload fails with 413 for anything
    # over nginx's 1MB default — on the one endpoint whose whole job is large
    # files.
    client_max_body_size 50m;

    # Exports and large file downloads stream. Buffering them in nginx costs
    # memory and delays the first byte; the timeouts are for a slow export,
    # not a slow service.
    proxy_buffering      off;
    proxy_read_timeout   300s;
    proxy_send_timeout   300s;

    # ⚠️ NO `add_header Access-Control-*` HERE. The application sends the CORS
    # headers, and nginx adding its own produces TWO
    # `Access-Control-Allow-Origin` values — which every browser rejects as
    # invalid, so "fixing" CORS at the edge breaks it instead.
}
```

Place both blocks above the general `location /api/` (or `location /`) block.
Order in the file does not matter for prefix locations — nginx picks the
longest match — but keeping them together and first is how the next person
finds them.

## 3. Flip it

```bash
cp /etc/nginx/sites-available/<site> /etc/nginx/sites-available/<site>.pre-crm
# edit in the two blocks above
nginx -t && systemctl reload nginx          # `-t` first, always
```

`nginx -t` is not optional: a syntax error plus a reload takes the whole site
down, CRM and platform alike.

## 4. Watch

```bash
python3 scripts/cutover_check.py --crm http://127.0.0.1:8100 \
    --host https://<public api host> --ui-origin https://<ui origin>

python3 scripts/watch_cutover.py --host https://<public api host> \
    --minutes 60
```

`watch_cutover.py` polls the public path with a real token, counts 5xx, and
prints a line a minute. What to watch for in the first ten:

* **a 404 on one screen** — a router that is not mounted. The gate checks
  12/12, so this would mean a path that differs rather than a missing include;
* **5xx on one endpoint** — most likely a serialisation fault the shimmed
  suite cannot see (a raw `ObjectId` in an untyped response is a 500; it has
  happened twice). The response body names the field;
* **every CRM screen failing at once, with the API answering fine to curl** —
  CORS. Roll back and fix `cors_origins`;
* **file upload 413** — `client_max_body_size` missing from the new block;
* **slow board** — an index. Both services declare the same ones and the CRM
  cannot drop any (`allow_index_dropping=False`), so check `pm2 logs` for
  index creation at boot instead.

## 5. Roll back

```bash
cp /etc/nginx/sites-available/<site>.pre-crm /etc/nginx/sites-available/<site>
nginx -t && systemctl reload nginx
```

That is the whole rollback: the platform still has every CRM route until
Phase 7, so traffic goes back to code that has been serving it all along.
**Nothing in the database needs undoing** — both services write the same
collections in the same shapes, so records created through the CRM service
are indistinguishable from records created through the platform.

⚠️ **This is the last moment the rollback is one line.** Phase 7 deletes the
CRM from the platform; after that, going back means a revert and a deploy.
Leave the dual run up for a few days before taking that step.

---

# Phase 7 — remove the CRM from the platform

Three steps, and **the order is the whole safety story**. Two of them are
additive and can go out today; the third cannot go out until the nginx rule
above is live and watched.

> ✅ **ALL THREE ARE APPLIED AND COMMITTED** in `emailMarketingAPI` (commits
> `a5b9826` … `7759c17`). This section is kept as the record of what was done
> and in what order — not as work still to do.
>
> A `platform-phase7/` directory used to sit in this repo holding
> `step-c-code.patch` and `step-c-remove-crm.sh`. Both were one-shot tools,
> both have been run, and it was deleted once the platform's own commits made
> them redundant. ⚠️ **TO UNDO ANY OF THIS, REVERT THOSE COMMITS** — do not go
> looking for the patch. It was generated against a baseline that has since
> moved (the `contacts.py` decorator fix landed after it), so re-applying it
> would not be clean.
>
> ⚠️ **AND REVERTING THE CODE IS NOT ENOUGH ON ITS OWN.** The nginx rule in §2
> above has to go back at the same time, or the platform serves CRM routes
> that nginx is still sending to this service.

## Step A — the platform answers "who is this caller?"  ✅ safe to deploy now

`emailMarketingAPI/api/routes/internal_auth.py` plus one `include_router` in
`api/routes_handler.py`, and `[MAIN] internal_api_key` in `config/app.conf`.

Purely additive: a new endpoint at `/api/v1/internal/auth/context` that
declares the platform's existing `get_request_context` and serialises the
result. Nothing else changes, and the CRM mounts in the platform keep
answering.

⚠️ **The nginx deny rule is part of Step A, not an afterthought.** The
endpoint resolves any caller's permissions; it must not be reachable from the
internet. The rule is in §2 above, and the header check is the second lock —
both, not either.

## Step B — the platform's one call into the CRM  ✅ safe to deploy now

`emailMarketingAPI/api/services/crm_client.py` plus
`[MAIN] crm_service_base_url`.

Also additive: nothing imports it yet. Leaving `crm_service_base_url` unset is
a valid state and means "no CRM service reachable" — the client logs once and
does nothing, which is what a rollout wants.

## Step C — delete the CRM  ⛔ NOT until the nginx rule is live

```bash
cd emailMarketingAPI
bash scripts/step-c-remove-crm.sh            # dry run first
bash scripts/step-c-remove-crm.sh --apply
git apply --3way docs/step-c-code.patch
git commit -m "Remove the CRM: it lives in CRM_API now"
```

**Why the order matters, in both directions:**

* nginx rule first, deletion second → nothing breaks. The platform's CRM
  mounts are simply unreachable, and the rollback above still works.
* deletion first, nginx rule second → **the CRM is gone.** 83 endpoints 404
  and every CRM screen in the app goes blank at once. Not a degraded corner:
  the whole module.

The script refuses to run if Step A/B is not in the tree, because
`internal_auth.py` is what the CRM calls to authorise *every* request.

### What Step C changes, and the one thing it keeps

| | |
|---|---|
| removes | the 16 `include_router` calls (83 endpoints), the 14 Beanie model registrations, the CRM task-reminder scheduler, and the six CRM trees + 20 CRM test/script files |
| rewrites | `_auto_link_company` in `api/routes/contacts.py` and the batch block in `api/routes/lists.py` — three CRUD constructions become one HTTP call each |
| moves | `tests/crm_integration/{harness.py,shims/,test_p2_contact_scope.py}` → `tests/platform_integration/`, with `scripts/platform_integration_tests.py` to run them |
| keeps | `api/services/contact_scope.py` and its 25 tests. **Platform code**, written during the CRM work, which is the only reason it sat in a directory called `crm_integration`. |

⚠️ **The task-reminder scheduler has to run in exactly one process**, and
which one is a CONFIG FLAG rather than a code change — deliberately, because
the cutover is the worst moment to be editing code. In the same deploy as
Step C:

```ini
# CRM_API/config/app.conf
[CRM]
task_reminders = true
```

Then check, rather than assume:

```bash
curl -s http://127.0.0.1:8100/health | python3 -m json.tool | grep -A3 task_reminders
# "configured": true, "running": true
```

If they are not the same deploy, reminders either stop silently — the platform
no longer has the models, and an unregistered Beanie Document returns nothing
and raises nothing — or both poll, which the data survives (each reminder is
claimed with a conditional update on `reminder.sent_at`) but which leaves a
customer one regression away from two reminder emails.

Anything but `1`/`true`/`yes`/`on` is off, so `task_reminders = ture` fails
closed.

### Verified before shipping

Applied to a throwaway clone of the platform at `0490a70` + Step A/B:

```
paths 560 (was 617)   crm []   internal ['/api/v1/internal/auth/context']
registry: 99 models, 0 CRM
platform_integration: ran 25  ✓
grep for routes.crm|engage.crm|crud.main.crm|models.main.crm|schemas.main.crm|enums.crm: nothing
```
