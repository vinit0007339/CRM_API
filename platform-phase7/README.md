# For the PLATFORM repo, not this one

Everything in this directory is applied to `emailMarketingAPI`. It lives here
because it is the other half of this service's boundary, and the two halves
have to be read together.

| file | where it goes | when |
|---|---|---|
| `step-c-remove-crm.sh` | `emailMarketingAPI/scripts/` | ⛔ after the nginx cutover |
| `step-c-code.patch` | `emailMarketingAPI/docs/` | ⛔ same commit as the script |
| `../docs/platform-internal-auth.py.txt` | `api/routes/internal_auth.py` | ✅ now (Step A) |

Step A and Step B are already written into `emailMarketingAPI` and are safe to
deploy: a new internal auth endpoint, a new unused HTTP client, two config
keys. The platform's CRM routes keep answering.

Step C is the deletion, and `docs/cutover.md` §Phase 7 is the runbook. The
short version:

> nginx rule first, deletion second → nothing breaks, rollback still one line.
> deletion first, nginx rule second → the CRM is gone, all 83 endpoints, at once.

The script refuses to run if Step A/B is not in the tree.

## The two config keys

`emailMarketingAPI/config/app.conf` — gitignored, so it is not in any commit:

```ini
[MAIN]
# Shared with this service, IN BOTH DIRECTIONS: the CRM sends it to the
# platform's /api/v1/internal/auth/context, and the platform sends the same
# value to the CRM's /api/v1/internal/crm/auto-link. Generate once:
#     python3 -c "import secrets; print(secrets.token_urlsafe(48))"
internal_api_key = <the same value as the CRM's [CRM] internal_key>
# Where this service listens. Unset means "no CRM service reachable": the
# platform keeps working and auto-link pauses.
crm_service_base_url = http://127.0.0.1:8100
```

And `config/app.conf` in THIS repo needs the same secret, under the **`[CRM]`**
section — not `[MAIN]`, and not the same key name:

```ini
[CRM]
platform_base_url = http://127.0.0.1:8000
internal_key = <the same value as the platform's [MAIN] internal_api_key>
```

⚠️ **One secret, two names, two sections.** `[MAIN] internal_api_key` in the
platform, `[CRM] internal_key` here. They must hold the same value; a mismatch
produces a 404 from both internal endpoints, which deliberately do not confirm
their own existence to a caller without the key — so the symptom is "the
endpoint does not exist", not "the key is wrong". If you are debugging a 404
on either side, compare the two values first.
