# The host contract

What a project must provide to use this CRM. **Two endpoints, and one of them
is optional.** If you implement both, you get the whole module: companies,
deals, pipelines, the Kanban board, notes, tasks, files, custom attributes,
saved views, imports and exports — 83 endpoints under `/api/v1/crm/`.

The CRM owns 14 collections and writes to no others. It asks you for exactly
two things, and nothing else:

| | question | endpoint | required |
|---|---|---|---|
| **Identity** | who is this caller, and what may they do? | `GET /api/v1/internal/auth/context` | yes, unless you use `jwt_claims` |
| **Directory** | what is this user / tag / contact called? | `POST /api/v1/internal/crm-host/directory` | no — see `directory = none` |

`emailMarketingAPI` is the reference implementation of both:
`api/routes/internal_auth.py` and `api/routes/internal_crm_host.py`.

---

## 0. Pick how much CRM you want

```ini
[CRM]
profile = core        ; full (default) | core | minimal
```

| profile | endpoints | modules | you get |
|---|---|---|---|
| `full` | 83 | 12 | everything Engage has |
| `core` | 61 | 7 | companies, deals, pipelines + board, notes, tasks |
| `minimal` | 47 | 5 | records, pipelines + board |

`core` leaves out the machinery Engage grew around the CRM: saved views,
per-user column layouts, the import wizard, export jobs, and files with
signed downloads. Each is a feature you can switch on later by moving to
`full` — not a thing you must understand on day one.

⚠️ **`attributes` and `settings` are in every profile, and not because they
are small.** `deals` routes every custom field value through
`attribute_routing`, which reads attribute *definitions*; and `settings`
carries `lost_reasons`, without which closing a deal as lost cannot render.

⚠️ **This trims the API surface, not the source tree.** The repository is the
same size under every profile. There is one codebase on purpose —
`app/profiles.py` has the argument, and it is short: a forked CRM is two
copies of a tenancy decision with a two-year fuse.

`GET /health` reports which profile a box is running, so "why is
`/crm/imports` a 404 on staging?" is one curl and not two config files.

## 1. Pick your adapters

```ini
; CRM_API/config/app.conf
[HOST]
identity  = introspect        ; or jwt_claims
directory = shared_db         ; or http, or none
base_url  = http://127.0.0.1:8000
internal_key = <a shared secret, 48+ random chars>
```

| if your project… | identity | directory |
|---|---|---|
| already has accounts, roles and permissions | `introspect` | `http` |
| has an auth server whose tokens already carry account + permissions | `jwt_claims` | `http` |
| shares one Mongo with the CRM (the platform does) | `introspect` | `shared_db` |
| is starting out and just wants it running | `jwt_claims` | `none` |

⚠️ **The defaults are `introspect` + `shared_db`**, which is the platform's
configuration. A deployment with no `[HOST]` section keeps working exactly as
before.

---

## 2. Identity — `GET /api/v1/internal/auth/context`

The CRM verifies the token's **signature** itself (RS256, your public key in
`[CRM] jwt_public_key`). This endpoint answers the other half: which account,
and what may they do.

**Request.** The CRM forwards the end user's own credentials:

```http
GET /api/v1/internal/auth/context
Authorization: Bearer <the end user's token, verbatim>
X-Account-Id: <the account header the user sent, if any>
X-Internal-Key: <the shared secret>
```

**Response — 200.**

```json
{
  "status": "success",
  "message": "",
  "data": {
    "account_id": "6a80479c2ee98febd40f58de",
    "role": "manager",
    "is_owner": false,
    "extra_grants": ["crm_deals:write"],
    "denials": ["crm_companies:delete"],
    "custom_roles": {"sales": ["crm_deals:read", "crm_deals:write"]},
    "builtin_overrides": {},
    "is_api_key": false,
    "user": {
      "id": "6b11...", "email": "priya@acme.com",
      "first_name": "Priya", "last_name": "S",
      "username": "priya", "picture_url": null
    }
  }
}
```

Only `account_id` is load-bearing. Everything else may be omitted: no
`role` means `owner`, no `extra_grants` means none, and the CRM's own RBAC
(`app/auth/rbac.py`) resolves permissions from these fields.

⚠️ **`account_id` is the tenancy boundary.** Every CRM record is scoped by
`created_by == account_id`. Returning the wrong one hands a caller another
tenant's records, and returning `null` matches records whose `created_by` is
unset. If you cannot determine the account, answer 403 — never 200 with a
null account.

⚠️ **Decide `X-Account-Id` yourself, do not trust it.** A user may belong to
several accounts, so the CRM forwards whichever one the browser asked for. It
is your job to check they may act for it. If you echo it back unchecked, any
valid token can read any account.

**Rejections.** Answer `401` for a bad credential and `403` for a valid
credential that may not act for this account. The CRM passes the status
through unchanged, so the two services answer identically.

⚠️ **Do not answer 500 or time out for an authorisation decision.** The CRM
treats an unreachable host as `503`, not `401` — on purpose, because
answering `401` would sign every user out of the CRM during a blip. But a host
that is *up* and 500s on a permission question produces a CRM that is down
with no explanation. Decide, or say 403.

**Caching.** The CRM caches the answer for 30 seconds, keyed on a SHA-256 of
the credential. A revoked permission takes effect within 30 seconds, not
immediately. If that is too slow for you, say so — it is one constant.

### Or skip it entirely: `identity = jwt_claims`

No endpoint. The CRM reads the already-verified token:

| field | claim | fallbacks |
|---|---|---|
| `account_id` | `account_id` | `org_id`, `tid` |
| `role` | `role` | — (default `owner`) |
| `extra_grants` | `permissions` | `scope` (space-separated) |
| `user.id` | `sub` | `user_id` |
| `user.email` | `email` | — |

Rename any of them in a `[HOST_CLAIMS]` section:

```ini
[HOST_CLAIMS]
account_id = https://acme.example/claims/org
```

⚠️ **This moves the tenancy decision to whoever mints the token.** Your auth
server now owns "which account is this, and what may they do", and a token
stays valid until it expires — a revoked grant is honoured until then.
Right if your tokens already carry these claims; wrong if you would have to
invent them to fill the fields.

⚠️ **`X-Account-Id` is ignored under this adapter**, because there is nobody
to ask whether the caller may use it. The token's own claim is the only
source. A multi-account user therefore needs a token per account.

---

## 3. Directory — `POST /api/v1/internal/crm-host/directory`

Optional. It supplies the **names behind ids the CRM stores but does not
own**: the owner beside a deal, a company's tag chips, a company's Contacts
tab.

**Request.** One call per kind per page — never one per row.

```json
{"kind": "users", "ids": ["6b11...", "6b12..."]}
```

`kind` is `users`, `tags` or `contacts`. For a contact lookup by address:

```json
{"kind": "contacts", "account_id": "6a80...", "email": "asha@acme.com"}
```

**Response — always 200 when you understood the request.**

```json
{"status": "success", "message": "", "data": {"rows": [
  {"_id": "6b11...", "first_name": "Priya", "last_name": "S",
   "email": "priya@acme.com"}
]}}
```

`_id` is required on every row. Everything else is best-effort: the CRM reads
`first_name`/`last_name`/`email` for users and contacts and `name`/`colour`
for tags, and renders what it gets.

### The two failure rules, and why they differ

This is the only genuinely subtle part of the contract.

**Expansion** — `users`, `tags`, and `contacts` by id for a Contacts tab — is
**cosmetic**. If you cannot answer, the CRM logs it and renders ids instead of
names. A page of deals must not fail because a name lookup did; the deals are
the data, the names are decoration.

**Lookup** — `contacts` by `email`, or by a single id to resolve a note's
target — is **load-bearing, and a wrong answer creates data**. The CRM's
import worker reads it as *"does this person already exist?"* and **creates a
contact when the answer is no.**

⚠️ **So never answer "no rows" when you mean "I could not check."** A 404,
a 500, a timeout or a 200 with a surprise shape are all treated as failures
and raise — deliberately, because collapsing them into "no such contact"
turns a thirty-second outage into a column of duplicate people in a
customer's account, with nothing in any log to say why. Answer `200` with
`rows: []` **only** when you genuinely looked and found nothing.

⚠️ **Scope the email lookup to `account_id`.** If your contact rows are shared
between accounts behind a globally unique email index — the platform's are —
an unscoped lookup attaches one tenant's note to another tenant's person.

### Or skip it: `directory = none`

Companies, deals, pipelines, the board, notes, tasks, files, attributes,
saved views, imports and exports all work completely. What degrades is
cosmetic: an owner shows as an id, tag chips are absent, a company's Contacts
tab is empty. Note- and task-targeting by contact is unavailable, which is a
feature a project without contacts does not have anyway.

This is a reasonable way to start. Bring the CRM up, click through it, then
implement the directory when you want names.

---

## 4. What the CRM asks of you, and nothing more

⚠️ **There is no `write_*` in this contract, now or ever.** The CRM never
writes to a collection it does not own. If it ever needs to change one of your
contacts, that is a new endpoint you add deliberately — not a query it makes.

The interface is five reads, enforced by a test
(`tests/test_host.py::test_the_host_surface_is_still_exactly_five_reads`), so
it cannot grow by accident:

```
contact_by_email(account_id, email)   LOOKUP    scoped
contact_by_id(account_id, id)         LOOKUP    scoped
contacts_by_ids(ids)                  EXPANSION
users_by_ids(ids)                     EXPANSION
tags_by_ids(ids)                      EXPANSION
```

## 5. The other direction

One thing the CRM offers back, which you may ignore:
`POST /api/v1/internal/crm/auto-link` takes a batch of `(contact_id, email)`
and links each to a company derived from the email domain. The platform calls
it after creating a contact or importing a list —
`emailMarketingAPI/api/services/crm_client.py` is the reference client.

⚠️ **It takes a batch because the batch is the contract, not an
optimisation.** Five thousand contacts at the same company must be one
company lookup, not five thousand, and must not race into creating that
company twice. Calling it per contact in a loop undoes both.

## 6. Both internal paths must be denied from outside

```nginx
location /api/v1/internal/ {
    allow 127.0.0.1;
    allow ::1;
    deny all;
    proxy_pass http://127.0.0.1:8000;
}
```

Place it **above** your general `/api/` block; nginx picks the longest
matching prefix.

⚠️ **Both locks, not either.** The `X-Internal-Key` header is the second;
the network rule is the first. A header leaks into access logs and bug
reports; an nginx config gets edited by someone who does not know why a rule
is there. Neither is load-bearing alone. And an **unset** key makes both
endpoints refuse outright rather than run open — an internal endpoint that is
accidentally public tells you nothing, because there is no error and no log
line, just an account id anyone can act on.
