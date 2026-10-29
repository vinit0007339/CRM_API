# Calling the CRM from your own backend

For a server-to-server caller — your website's backend, a script, an
integration — rather than a browser.

---

## The credential

A platform API key, minted at `POST /api/v1/developer/api-keys` on
`emailMarketingAPI`. It looks like `eng_live_<43 chars>`, is shown **once**,
and is stored only as a SHA-256 hash.

It travels in the ordinary header. There is no `X-Api-Key`:

```
Authorization: Bearer eng_live_…
```

⚠️ **A key belongs on a server, never in a page.** In browser JavaScript it is
readable by anyone who opens the network tab, and it can write to the account.
A static site needs either the onsite form embed or a one-function proxy that
holds the key server-side.

### Scopes

A key carries the intersection of **what was scoped onto it** and **what its
creator can do right now** — so demoting the creator narrows every key they
issued, without anyone hunting for them.

For the CRM, scope the key to what it actually needs:

| | |
|---|---|
| `deals:read`, `deals:write` | create and read deals |
| `companies:read`, `companies:write` | create and read companies |
| `pipelines:read` | look up pipeline and stage ids |
| `contacts:read`, `contacts:write` | CRM-owned contacts |

⚠️ `team:*`, `billing:*`, `settings:write` and `apikeys:*` can **never** be
scoped onto a key. A key also always has `is_owner = false` and a role that is
not a built-in one, so its scopes are the *only* thing granting it anything —
an unscoped key can do nothing at all.

---

## Creating a deal in a specific pipeline and stage

### 1. Find the pipeline and its stages — once, not per lead

```bash
curl -s https://<crm-host>/api/v1/crm/pipelines \
  -H "Authorization: Bearer $ENGAGE_KEY"
```

Each pipeline carries its `stages[]`, each with `id`, `name`, `order` and
`type` (`open` / `won` / `lost`). Cache these; they change only when somebody
edits the pipeline.

⚠️ **Both ids are optional.** Omit them and the deal lands in the account's
default pipeline, in its first **open** stage — which is usually what a new
lead wants, and means a lead is never rejected for naming a stage that was
renamed.

### 2. The company, if you have a name

There is **no find-or-create by name**, so this is its own call. `primary_company_id`
is resolved by id and **silently ignored if it does not exist** — a wrong id
costs you the link, not an error.

```bash
curl -s -X POST https://<crm-host>/api/v1/crm/companies \
  -H "Authorization: Bearer $ENGAGE_KEY" -H 'Content-Type: application/json' \
  -d '{"name": "Acme Ltd", "domain": "acme.com"}'
```

⚠️ **Check for an existing company first** (`GET /crm/companies?q=acme`) or you
will accumulate one company per enquiry. "Acme", "Acme Ltd" and "ACME" are
three different companies as far as this endpoint is concerned.

### 3. The deal — one call does the links too

```bash
curl -s -X POST https://<crm-host>/api/v1/crm/deals \
  -H "Authorization: Bearer $ENGAGE_KEY" -H 'Content-Type: application/json' \
  -d '{
        "name": "Quote — Priya Sharma",
        "pipeline_id": "<pipeline id>",
        "stage_id": "<stage id>",
        "amount": 1200,
        "owner_id": "<user id>",
        "source": "api",
        "primary_company_id": "<company id>",
        "company_ids": ["<company id>"],
        "contact_ids": ["<crm contact id>"],
        "custom_fields": {"enquiry_message": "needs it by March"}
      }'
```

`name` is the only required field. The associations are written **inline** — no
separate `POST /crm/deals/{id}/links` call.

---

## Responses

Every CRM endpoint answers the same envelope:

```json
{ "status": "success" | "failure", "message": "…", "data": … }
```

⚠️ **A business failure is HTTP 200 with `status: "failure"`.** Checking the
HTTP status alone will read "that pipeline no longer exists" as a success.
Check `status`.

| Code | Meaning |
|---|---|
| **200** `success` | done |
| **200** `failure` | rejected for a business reason — read `message` |
| **401** | the key is unknown, revoked, or malformed |
| **403** | the key is valid but lacks the scope for this endpoint |
| **503** | the CRM could not reach the platform to check your key. ⚠️ **Retry; do not treat as an auth failure.** This is deliberately not a 401, so a platform blip does not look like a bad credential |
| **422** | the body did not validate — `{"success": false, "messages": [...]}` |

---

## Idempotency — yours to handle

There is none built in. Post the same lead twice and you get two deals.

The CRM has no natural dedupe key for this: a deal's name is not unique and
nothing on it points back at your record. Either keep your own
"already-sent" marker, or put your id in `custom_fields` and search before
posting.

---

## What this cannot do

| | |
|---|---|
| Create a **platform** (marketing) contact | A different endpoint — `POST /api/v1/lists/{list_id}/contacts` on the platform, which also subscribes them and starts any `list_join` automation. A deal created here does **not** put anyone on a marketing list |
| Find or create a company by **name** | id only; see step 2 |
| Move a deal's stage with the same call | `PATCH /crm/deals/{id}/stage` |
| Work from browser JavaScript | see the warning above |

---

## How this works, for whoever maintains it

The CRM authenticates in two steps: verify the RS256 session JWT locally,
**then** introspect it at the platform. Both, always — the CRM does not rely on
the platform alone to decide a signature is good.

An API key has no signature the CRM holds a key for and no claims to read, so
the local step is not skipped, it **cannot exist**: the platform is the only
thing that can say whether a key is real. `app/auth/context.py` therefore sends
an `eng_live_…` bearer straight to introspection, and the platform's
`/internal/auth/context` answers for it exactly as it does for a token —
returning `is_api_key`, which the CRM records on the request context.

A key's scopes arrive as `extra_grants` and `rbac.has_permission` unions them
into the role's permissions, which is why `require("deals:write")` passes for a
scoped key whose role grants nothing.

⚠️ **On a `jwt_claims` deployment an API key is refused with a 401**, by
design: that adapter resolves a caller by reading claims out of a verified
token and has no host to ask, so there is nothing a key could be checked
against. `ACCEPTS_OPAQUE_CREDENTIAL` on each adapter is what decides this, and
the refusal is deliberate rather than an attempt that fails — attempting it
raises from inside `resolve()` and would surface as a 500.
