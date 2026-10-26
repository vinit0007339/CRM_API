# Phase 5 — the dual run

Both services serving the CRM, nginx still pointing at the platform, and a
tool that proves they agree. Nothing user-visible changes in this phase; that
is the point of it.

**Exit criterion:** `scripts/diff_against_platform.py` reports zero persistent
differences across at least three real accounts, including one with a full
pipeline and one with almost no data.

---

## 1. Before anything is deployed

The platform needs the auth endpoint from Phase 4, or every CRM request to the
new service answers 503:

```bash
# in emailMarketingAPI, from docs/platform-internal-auth.py.txt
#   · api/routes/internal_auth.py
#   · the include_router call in api/routes_handler.py
#   · [MAIN] internal_api_key = <a long random string>
#   · the nginx deny rule for /api/v1/internal/
curl -i -H "X-Internal-Key: $KEY" -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8000/api/v1/internal/auth/context       # expect 200
curl -i https://<stage-host>/api/v1/internal/auth/context      # expect 403
```

Both curls matter. The first says the endpoint works; the second says the
internet cannot reach it.

## 2. Deploy the CRM service beside the platform

```bash
ssh root@<droplet>
cd /var/emailMarketingAPI-stage
git clone <CRM_API remote> CRM_API && cd CRM_API

cp config/app.conf.example config/app.conf
# [MAIN_DATABASE]  the SAME db_uri and db_name as the platform — one database,
#                  two owners (app/db.py says why)
# [CRM]            port 8100
#                  jwt_public_key_path → the platform's own
#                    config/jwt/jwt_rs256.key.pub
#                  platform_base_url  → http://127.0.0.1:8000
#                  internal_key       → the platform's internal_api_key

poetry install --no-root
pm2 startOrRestart ecosystem.config.js && pm2 save
python3 scripts/smoke.py
```

`smoke.py` must print `routers 12/12`, `endpoints 83/83` and a reachable
database. If it prints an auth line, stop and fix the config: a service that
cannot authenticate will diff as "everything differs".

⚠️ **The CRM service must NOT be in nginx yet.** It listens on 127.0.0.1:8100
and only the diff tool talks to it. Phase 6 is the one-line nginx change, and
keeping these separate is what makes the rollback a single reload.

⚠️ **Expect both services to create indexes on boot.** Beanie does that at
`init_beanie`, and both register the same 14 models against the same database.
It is idempotent; the log lines are not a problem. The CRM service runs with
`allow_index_dropping=False` precisely so it cannot remove an index the
platform declares (see `app/db.py`).

## 3. Run the diff

```bash
export CRM_DIFF_TOKEN="<an access token for a real account>"
python3 scripts/diff_against_platform.py \
    --platform http://127.0.0.1:8000 \
    --crm      http://127.0.0.1:8100 \
    --account  <account id> \
    --report   /tmp/diff-<account>.json
```

It issues **only GETs** — both services share one database, so a compared POST
would write twice. It discovers real pipeline, company and deal ids from the
platform, then compares roughly 50 endpoints including the board, the list
views, notes, tasks, files, activities, saved views, attributes, settings, and
two deliberately bad ids so the FAILURE shapes are compared too.

Run it for at least three accounts:

| account | why |
|---|---|
| your biggest CRM user | pagination, a full board, custom attributes, saved views |
| a recently created one | the self-provisioning paths — default pipeline, system attributes |
| one with almost no CRM data | an empty account makes everything pass, so it proves the tool is wired up, not that the services agree. Run it, but never *only* it |

### Reading the output

| mark | meaning | what to do |
|---|---|---|
| `✓` | byte-identical | nothing |
| `~` | differed once, matched on re-check | live data moved between the two calls. Note it; if one path keeps flickering, re-run with `--retries 3` |
| `✗ status` | different HTTP code | usually an envelope mismatch — one service answering 404 where the other answers a business failure at 200 |
| `✗ type` | same value, different JSON type | the ObjectId-in-an-untyped-field class. `1` vs `1.0`, `"abc"` vs `{"$oid":…}` |
| `✗ order` | same elements, different order | a sort key, not a serialiser. Some CRM lists sort deliberately |
| `✗ value` | genuinely different data | read it carefully: a timestamp difference here means the two serialise datetimes differently |
| `✗ length` | one list is shorter | a filter or a scope differs. The most serious kind |

Each `✗` is either a porting bug or a deliberate change nobody wrote down.
There is no third option, and "it is probably fine" is not a finding.

## 4. What the diff cannot tell you, and what to do about it

The tool reads. These need a human on the stage UI, pointed at the CRM
service — easiest with a temporary nginx rule on a staging hostname, or by
running the UI locally against `http://127.0.0.1:8100`:

* **create, update, archive, merge, bulk** — every write path. Do one of each
  on stage and confirm the record afterwards.
* **file upload and download**, including the `?disposition=inline` preview:
  it is the one endpoint that streams rather than returning JSON.
* **import and export**, which run in a background task — start one of each
  and watch it finish.
* **the task-reminder scheduler**, which is deliberately NOT started in the
  CRM service yet. The platform still owns it until Phase 7; if you see
  reminders going out twice, that is the thing to check.

## 5. If a difference is real

Fix it in this repo, re-run the one endpoint, then the whole set:

```bash
python3 scripts/diff_against_platform.py --only "/crm/deals" ...
python3 scripts/crm_tests.py            # and add the test that would have caught it
```

⚠️ **Add the test before moving on.** A difference the diff found and the suite
could not is a hole in 1,735 tests, and the diff only runs during this phase.
Once nginx is switched, the suite is all that is left.

Do not reach for `IGNORED_PATHS`. It exists for a field that one day
legitimately differs, it is empty today, and `tests/test_diff.py` fails if
anything is added without a reason in the dict.
