# How a lead gets into a pipeline — the Brevo model, applied to us

**Plan only. Nothing implemented.**

Written to answer one question: *"how does the lead automatically come into
any pipeline so that we can track it?"*

**Decision taken: we follow Brevo.** Payaca is out of this document — its
answer was structural (every enquiry becomes a project, always) and we are not
building that. Everything below assumes Brevo's model, which is a different
idea entirely and worth stating plainly before the plan:

> **Brevo has no lead-intake feature.** A lead becomes an ordinary marketing
> contact through ordinary marketing intake. A **step inside an automation**
> then turns that contact into a deal. There is no "lead" object, no intake
> pipeline, and no setting called "send leads to the CRM".

So the thing to build is **not** a lead importer. It is one automation step.

§2.3 answers the second follow-up — *if we do not use Landing Pages and Signup
Forms, is there another way into a specific deal or pipeline?* **Yes: two, and
one of them is a single conditional that skips most of this plan.** Read §2.3
first if that is the live question.

§2.0 answers the earlier follow-up — *how does a lead come in from any
website, and then flow to a deal or a company* — now that Landing Pages and
Signup Forms are going to production. Short version: **the capture works, and
four things between it and a pipeline do not.** Three of them are platform-side
and none of them is in the CRM.

Sources: Brevo's published automation docs, read 28 Oct 2026 (their app is
behind a login I can't use). Our side is measured — every claim names a file
and a line. Supersedes the earlier version of this document, which weighed
Payaca's model as an alternative.

---

## 1. The answer, in one picture

```
BREVO                                   US, TODAY
form on the customer's website          form on the customer's website
     │                                       │ <script src="…/onsite/forms.js">
     │                                       │ multipart POST /f/{slug}/submit  ✅
     ▼                                       ▼
  CONTACT, subscribed                   platform `contacts`, subscribed=FALSE
     │                                       │
     │                                       │ ⚠️ waits for a confirmation CLICK
     │                                       │    before it joins any list
     │                                       ▼
     │                                  on no list → nothing happens, ever
     │                                       │
     │ entry point fires                     │ joins a list (on confirm)
     ▼                                       ▼
  automation runs                        list_join flow runs  ✅ this works
     │                                       │
     │ step: "Create a deal"                 │ steps: email · wait ·
     │  · pipeline + stage                   │        condition · split · exit
     │  · name from the contact              │
     │  · owner = contact's owner            ▼
     ▼                                   ╳  there is no CRM step
  DEAL in a pipeline                     ╳  the CRM has no door
                                         ╳  the platform has no caller
```

The left column is four moving parts and **we already have three of them** —
the gap to a deal is narrower than it has looked for four phases.

But the right column shows the part your question is really about: between a
form on someone's website and a contact that can trigger anything, there are
**four blockers, three of which are not CRM work** (§2.0). A lead that never
joins a list cannot become a deal no matter how good the CRM step is.

---

## 2. Brevo's flow, mapped piece by piece onto ours

| | Brevo | Ours | Status |
|---|---|---|---|
| 0 | A form on the customer's own website | onsite embed + iframe, both work cross-origin | ✅ capture · ⚠️ §2.0 |
| 1 | A lead becomes a contact | 12 inbound routes write platform `contacts` | ✅ but `subscribed=False` |
| 2 | An entry point fires an automation | `list_join` is wired and swept | ✅ **works** — §2.1 |
| 3 | The contact walks the automation | `flow_executor` + `FlowTriggerLog` | ✅ |
| 4 | Step **"Create a deal"** | nothing — 4 step types exist | ❌ **the one missing step** |
| 5 | The deal lands in a pipeline | `POST /crm/deals` exists and works | ✅ (no caller can reach it) |
| — | *(same system, one database)* | two services, needs a door + a caller | ❌ §3 |

### 2.0 First: how a lead gets in from *any* website

Landing Pages and Signup Forms are built but not in production, so this is the
part to get right before anything downstream. **A customer can already put a
form on their own site**, three ways, and the mechanics differ in ways that
matter.

| | What the customer installs | Works on their own domain? |
|---|---|---|
| **Onsite embed** (the real one) | `<script src=".../onsite/forms.js?company_id=<id>">` plus `<div id="engage-form-<slug>">` | ✅ yes |
| **iframe** | `<iframe src="<frontend>/f/<slug>">` | ✅ yes (no CORS at all) |
| Hosted page | a link to `/f/<slug>` or `/p/<slug>` | ✅ not their site |

The embed is served by `GET /api/v1/content/onsite/snippet`
(`content_builder.py:2402`, built at `:2410-2418`) and the install screen with
the copy button already exists —
`emailMarketingUI/src/screens/Engage/SignupForms/OnsiteTracking.jsx:177-236`.
`forms.js` fetches `/onsite/forms-config?company_id=…`, injects the form's
compiled HTML, and on submit POSTs a `FormData` to the absolute `submit_url`
(`wireSubmit`, `content_builder.py:2203-2241`).

So the capture half is real. **Four things stand between it and a deal**, and
three of them are not CRM work at all.

#### ⚠️ Blocker A — a website lead does not join a list until it clicks a confirmation email

All four public submit paths create the contact with `subscribed=False` and
**put it on no list**. The list ids are stashed on a pending token
(`content_builder.py:1783`, `:1859`) and the membership is written only by
`POST /api/v1/content/landing-pages/confirm` (`:895-966`).

Since §2.1's trigger is `list_join`, **the deal cannot exist until the lead
clicks the email.** For a newsletter that is correct and legally sound. For
"request a quote" it is wrong: sales should see that lead immediately, and a
B2B enquiry is not a marketing subscription.

And the toggle that should control this **is read by nothing.**
`SignupFormModel.double_opt_in` exists (`signup_form.py:49`), the editor
exposes it (`SignupFormEditor.jsx:677`), the API stores it
(`content_builder.py:1239`) — and no submit path ever reads it. Double opt-in
is unconditional; turning the switch off changes nothing.

The decision logic is already written **and tested**:

```python
# api/services/engage/compliance.py:206
def requires_double_opt_in(*, enabled: bool, source: Optional[str] = None) -> bool:
```

…exempting `csv` / `import` / `migration`, with assertions in
`tests/test_engage_compliance.py:196-204`. It has **no caller outside those
tests**. So honouring the toggle is wiring one tested function into four submit
paths, not designing a feature.

⚠️ **This is a compliance decision, not a UX one.** Skipping confirmation means
emailing someone who only typed their address once, which is what double opt-in
exists to prevent. The safe shape: an enquiry form may skip DOI **for CRM
intake** — the deal is created, sales may reply — while the contact stays
`subscribed=False` and marketing may *not* send to it until it confirms. Lead
and subscriber become two different states, which is what they always were.

#### ⚠️ Blocker B — a website form cannot send a company, because contacts have no company field

`Contact` has no company or organisation field at all
(`app/db/models/main/contacts.py:18-90`).

A form field named `company` is not reserved
(`_RESERVED_FORM_FIELDS`, `content_builder.py:63` — `email`, `first_name`,
`last_name`, `name`, `phone`, `whatsapp`, `opt_in`, `ref`), so
`_collect_submission_fields` (`:66-85`) puts it in
`Contact.custom_fields["company"]` as a flat string.

Three consequences for "flow into the pipeline **or company**":

* The company name is **zero-party string data under whatever key the form
  author typed**. The builder's "Field name" box
  (`SignupFormEditor.jsx:1451-1458`) is unvalidated free text with no attribute
  picker, so `company`, `Company`, `company_name` and `organisation` are four
  unrelated keys.
* Typing a reserved name into any field **silently hijacks** a first-class
  contact attribute; typing `Phone` with a capital P silently does not.
* **The domain linker is the only company source that works.**
  `auto_link_service.py` (dead but intact) finds-or-creates a company from the
  email domain — and its own blocklist means a `gmail.com` lead gets no
  company. For a B2B enquiry form, the typed company name is better data than
  the email domain, and today it is unreachable.

Also lossy, worth knowing before relying on form data: on an **existing**
contact, `first_name` / `last_name` / `phone` are written only if currently
blank (`:1787-1792`), so a returning lead who corrects their phone number is
ignored. And `POST /api/v1/content/signup-forms/public/{id}/submit` declares a
`fields: Dict[str, Any]` body and **discards it** — `:1600` is literally
`captured = {}  # JSON submit carries no extra form fields`, with no log line.
(That route is also unused by the UI.)

#### ⚠️ Blocker C — the CORS split: form submits work, JSON tracking does not

There is one CORS config, duplicated at `api/main.py:427-449` and
`api/maincon.py:375-397`. It allows our own hosts plus
`allow_origin_regex=r"http://localhost:\d+"`. **An arbitrary customer domain
is not in it.**

The public endpoints work around this with a manual wildcard —
`_CORS_OPEN = {"Access-Control-Allow-Origin": "*"}` (`content_builder.py:2005`)
via `_cors_json` — and the split falls exactly on the content type:

| Endpoint | Body | Preflight? | From a customer domain |
|---|---|---|---|
| `POST /f/{slug}/submit` | multipart (FormData) | **no** — safelisted | ✅ works (`_cors_json` at `:1878`) |
| `POST /l/{slug}/submit` | multipart | no | ✅ works (`:833`) |
| `GET /onsite/forms-config` | — | no | ✅ works (`:2038`) |
| `POST /onsite/identify` | **JSON** | **yes** | ❌ `CORSMiddleware` answers the `OPTIONS` first and refuses the origin — the handler's `ACAO: *` never runs |
| `POST /onsite/track`, `/onsite/form-view` | JSON | yes | ❌ same |

So **lead capture works cross-origin, and only because `forms.js` posts
multipart.** Impression tracking, `window.engage.identify()` and
`window.engage.track()` fail silently on every real customer site. That is not
on the critical path for a deal, but it means the onsite feature is half dark
in production and the analytics will look wrong.

⚠️ And a second one that will cost support time: error responses from
`/f/{slug}/submit` are raised as `HTTPException` (`:1740`, `:1771`), which
bypass `_cors_json` and carry no CORS header — so the browser blocks the body
and `forms.js` shows `alert('Network error.')` (`:2239`) instead of the real
message. Every diagnostic on a customer's site reads as a network failure.

#### ⚠️ Blocker D — confirming does not fire the trigger, and a list-less form fires nothing ever

The confirm handler writes `list_memberships` with a raw motor upsert
(`content_builder.py:941-962`), bypassing `ListMembershipCrud.add_contact` —
so it never calls `trigger_contact_through_list_flows`, whose only caller in
the codebase is `lists.py:1431`. The sweep still picks the contact up (§2.1),
so this is **latency, not loss**: up to one scheduler tick, ~2 minutes
(`api/main.py:276`).

The real hazard is the list being absent. A **landing page's list is
optional** — the creation dialog says "Subscriber list (optional)"
(`LandingPageEditor.jsx:767-773`) — and a signup form's list is enforced
**only in the browser** (`SignupFormEditor.jsx:661-664`); the API accepts
`list_id: None` (`content_builder.py:361`) and `POST /signup-forms/{id}/status`
flips a form to active with no list check at all (`:1247-1261`).

With no list: `list_ids == []`, the membership block is skipped, and the lead
lands in `contacts` subscribed, **on no list, in no list view, triggering no
flow — ever.** The visitor sees "Thanks for subscribing!". For landing pages
that is the *default* state.

⚠️ So a deal can be silently impossible because of a dropdown nobody filled in
on a different screen. Any lead-to-deal feature has to say so at configuration
time, not fail quietly at runtime.

### 2.1 The trigger we need already fires — this is the finding that matters

I expected to have to build an entry point. **We do not**, and here is why.

Brevo's headline entry point is *"A contact submits a form"*. We have no
`form_submission` trigger — it is not even in the declared vocabulary
(`custom_flow.py:104-110` lists nine triggers and that is not one of them).
But a form submission **joins a list**, and `list_join` is one of only two
triggers that are actually wired.

And it fires regardless of how the contact got onto the list, because
`flow_scheduler._process_flow` pages through **every subscribed list member**
on each tick (`flow_scheduler.py:212-230`) rather than reacting only to an
event. `FlowTriggerLog` has a **unique index on `(flow_id, contact_id)`**
(`flow_trigger_log.py:85`) — one enrolment per contact per flow — so the sweep
enrols each member exactly once and never twice.

⚠️ **One wrinkle, and it is latency rather than a gap.** The immediate hook
`trigger_contact_through_list_flows` is called from exactly one place,
`lists.py:1431`, the *authenticated* add-to-list route. The public opt-in
confirm path writes `list_memberships` with a raw upsert
(`content_builder.py:~935`, "Mirrors the CSV-import membership write") and so
misses the hook. Those contacts are still picked up — by the sweep, on the
next tick. So a form-driven deal is created minutes late, not never.

**Consequence for this plan: step 4 is the whole job.** No new trigger, no
scheduler change, no event contract.

### 2.2 What Brevo's "Create a deal" step actually configures

To copy, we need their field list. From their docs, verbatim where quoted:

| Field | Brevo's behaviour |
|---|---|
| **Deal name** | defaults to the contact's first name, last name, email or phone; custom text can be added, e.g. "a prefix identifying the source" |
| **Pipeline** | chosen in the step; must exist beforehand |
| **Deal stage** | chosen in the step |
| **Assignment** | defaults to **the owner of the contact** who entered the automation. For a different owner or a rotation, you add an *"Assign a user to the contact"* step **before** the create step |
| **Follow-up task** | optional: a to-do with a due date and duration, assigned to the contact's owner |
| **Amount** | ❌ not settable |
| **Company link** | ❌ not mentioned |

Their other two CRM actions, for completeness: **"Create a task"** and
**"Assign a user to the contact"** — three of a fifteen-action palette.

Two of their limits are worth copying *deliberately* rather than by accident:

* **An automation cannot update a deal.** Their documented workaround is to
  store the value on the *contact* and set it with "Update contact attribute".
  So `Create a deal` is create-only; there is no `move stage` action.
* **No deal-stage entry point.** Their canonical entry-point list has nothing
  about deals, companies or stages. One article mentions "an automation after
  a deal stage is updated" which that list does not confirm.

---

### 2.3 If you don't use Landing Pages or Signup Forms — two shorter paths

**Yes, and one of them is shorter than this entire document.** The forms
feature is one way to capture a lead; it is not the only one. If the customer's
website has a backend that can hold a secret, two routes exist.

#### Path A — their backend posts to the list endpoint (works today, zero code)

```
their website form  →  their backend  →  POST /api/v1/lists/{list_id}/contacts
                                         Authorization: Bearer eng_live_…
```

`lists.py:1233` is the one ingress route that does everything the forms path
cannot:

* **`require("lists:write")`**, so an **API key works** — not just a browser
  session. Keys are real: `eng_live_<43 chars>` minted at
  `POST /api/v1/developer/api-keys` (`smtp_api_keys.py:269`), SHA-256 stored,
  carried in the ordinary `Authorization: Bearer` header
  (`auth_or_key.py:58-66`).
* **`subscribed=True` hard-coded** (`lists.py:1311`) — **no double opt-in,
  no confirmation click.** Blocker A of §2.0 does not apply.
* **Joins the list in the same call** (`lists.py:1403`) — blocker D does not
  apply.
* **Fires the flow immediately** (`lists.py:1428-1435`), not on the next
  2-minute tick, and `already_member` makes it idempotent.

So every §2.0 blocker except the company field disappears. A lead posted this
way is a list member with a flow running against it within the request. ⚠️ It
still needs the `crm_deal` step (§3.1) to become a deal — this path fixes
*capture*, not the CRM gap. And `ListAddContactRequest`
(`schemas/main/lists.py:113-135`) accepts **no `custom_fields`**, so a company
name has nowhere to ride.

#### Path B — let an API key reach the CRM directly (one conditional, and it skips most of this plan)

```
their website form  →  their backend  →  POST /api/v1/crm/companies   (if named)
                                      →  POST /api/v1/crm/deals
                                         pipeline_id + stage_id + contact_ids
```

This is the answer to *"get the data into a specific deal or pipeline"* taken
literally. `DealCreateRequest` (`app/schemas/deal.py:21-42`) already accepts
`pipeline_id`, `stage_id`, `amount`, `owner_id`, `source`, `custom_fields`,
`company_ids[]`, `primary_company_id` and `contact_ids[]` **in one call**, and
`deals.py:461-470` writes the associations inline — no separate `/links` call.

**It is blocked by exactly one thing.** `app/auth/context.py:127-135`:

```python
token = tokens.bearer(authorization)
tokens.verify(token)          # ← RS256 decode, BEFORE introspection
```

An `eng_live_…` key is not a JWT, so it fails here and 401s **before**
introspection is ever consulted. Everything downstream is already built for it:

* the platform's `GET /api/v1/internal/auth/context` resolves a key perfectly
  well and returns `"is_api_key"` (`internal_auth.py:101`) — its own docstring
  says *"API keys work from day one for the same reason"*;
* the CRM already forwards the raw header
  (`introspect.py:164` — `Authorization: authorization or f"Bearer {token}"`);
* the CRM already carries the marker —
  `api_key=object() if resolved.is_api_key else None` (`context.py:171`);
* and the **permissions resolve**, which was the real risk. A key's scopes
  arrive as `extra_grants` (`emailMarketingAPI/api/dependencies/context.py:133`)
  and the CRM's `has_permission` does `granted |= set(extra_grants)`
  (`app/auth/rbac.py:160`) — so `require("deals:write")` passes even though
  `role="api_key"` resolves to an empty set, which is exactly how that clamp
  was designed. `deals:write`, `pipelines:write` and `companies:write` are all
  already scopable onto a key (`key_rules` `NEVER_SCOPABLE` blocks only
  `team:*`, `billing:*`, `settings:write`, `apikeys:*`).

**So path B bypasses phases 1, 2 and 3 of this plan entirely** for a customer
who runs their own site: no CRM door, no `crm_client`, no flow-engine step, no
`flow_executor.py` ownership conversation. The plan's phases are for
*marketing-driven* intake — somebody fills in our form and walks a flow. A
backend that already knows it has a sales lead does not need any of that.

#### ⚠️ What path B does not solve

* **A static site cannot use it.** The key would sit in public JavaScript,
  where anyone can read it and write to the account. A pure HTML/JS site needs
  either our form embed (§2.0) or a one-function serverless proxy holding the
  key. **This is the deciding question**: does their site have a backend?
* **No company-by-name auto-create.** `primary_company_id` is resolved by id
  (`deals.py:461`) and silently ignored if not found; `primary_company_name` is
  only ever copied from a company that was fetched. A lead carrying a company
  *name* is `POST /crm/companies` first — two calls, three if the contact is
  new. §4.1(i) is the same decision, arriving from a different direction.
* **Packaging.** API keys are gated behind `require_api_keys_enabled`
  (`smtp_api_keys.py:38-70`) — `user.api_keys_enabled`, or a NevTan Workspace
  plan carrying `engage-api-access`. It is not sold as an Engage feature today,
  so "use the API" is a commercial decision before it is a technical one.
* **The marketing side stays empty.** A deal created this way has no platform
  contact unless the backend also posts one, so the person is not in `contacts`
  and cannot be marketed to. Path A and path B are complements: A for the
  marketing record, B for the deal.

#### The other routes, and why they are not the answer

| Route | Why not |
|---|---|
| `POST /api/v1/contacts` (`contacts.py:71`) | Works with a key, accepts a `list_id` and subscribes — but **triggers nothing** (`trigger_contact_through_list_flows` is not imported), so the flow waits for the sweep. Also refuses duplicate emails outright, so it is not idempotent. **No `custom_fields`** — the `attributes` field is commented out at `:39`. |
| `POST /api/v1/events` (`events_ingest.py:37`) | The **only** route that accepts `custom_fields` (`ingestion/service.py:169-186`), via `X-App-Id`/`X-App-Secret`. But it joins no list and fires no flow — and `custom_event`, though declared as a trigger, **is never executed**: the scheduler only ever fetches `list_join` (`flow_scheduler.py:670` hard-codes it). |
| `POST /api/v1/webhook/import` (`public_webhook.py:180`) | Looks like the Zapier door and **dead-ends**. It writes to `webhook_incoming_data` with a 7-day TTL and **nothing consumes that collection** — no worker, no scheduler. The `trigger/{event_id}` endpoint does not read it either; it makes an **outbound** GET to the customer's own URL (`user_webhooks.py:819-824`). The inbound token is also not shown anywhere in the UI. |
| `POST /api/v1/user-webhooks/trigger/{id}` | Inverted — we pull from their server — needs a person to invoke it, fires no flow, stamps `expires_at` +7 days, and **deletes prior `origin="webhook"` members of that list first** (`user_webhooks.py:632-641`). |
| Zapier / Make / n8n | **None exist.** Zapier is `status: "coming_soon"` in the integrations screen (`Integrations/index.jsx:254`). |

⚠️ **Unrelated but found while checking, and you should know:** `/docs`,
`/redoc` and `/openapi.json` are served **in production**. `api/main.py:410-424`
computes `_docs_on` correctly and then has all three arguments **commented
out**, so FastAPI falls back to its defaults. `api/maincon.py:358-372` has the
gate applied properly, but `dev_api.py:12` boots `api.main:app`. The CRM
service has no docs settings at all, so its `/docs` is open too. Not part of
this plan — worth a one-line fix on its own.

---

## 3. What is actually missing

Three things. One is the step; two are the wire between the services, and both
of those we deleted ourselves.

### 3.1 The step — `crm_deal` in the flow engine

`flow_executor.py:538-647` implements exactly **four** step types — `email`,
`exit`, `condition`, `split` — and ends:

```python
# tag / webhook / filter / sms / other — skip for now
return {"type": step_type, "skipped": True}
```

So `crm_deal` would be the fifth. The surrounding machinery is friendlier than
that line suggests:

* **The step gets the contact.** The executor is handed the contact document,
  so the step already knows the platform contact id and its owner — which is
  exactly Brevo's "owner of the contact who entered the automation".
* **Per-step state already exists.** `JourneyStepRecord`
  (`flow_trigger_log.py:30-40`) records `step_index`, `type`, `status`,
  `attempt` and `error` per step — so a `crm_deal` step records
  completed/failed like any other, and a retry can ask the journey whether it
  already ran instead of needing a new table.
* **The UI palette is one entry.** `FlowBuilder/index.jsx:130-139` — and note
  `tag` and `webhook` are sitting there commented out, which is the honest
  signal about this module's readiness.

⚠️ **This is work inside `emailMarketingAPI`, which is the CRM-417 ownership
question again — but a much smaller ask.** CRM-417 is blocked on a *contract*:
its §3.2 says `(flow_id, contact_id)` cannot express a CRM event, because a
deal event is not about a contact. A `crm_deal` step needs none of that: it is
additive code, it is about a contact, and `(flow_id, contact_id)` is exactly
the right key. Whether we may touch `flow_executor.py` is a people question;
it is not the same technical blocker, and it should not be bundled with it.

### 3.2 The CRM has no door — phase 4a removed it

`app/main.py:479` still carries the comment of the deleted router, cut off
mid-sentence, with the `include_router` gone:

```
# ⚠️ THE INTERNAL ROUTER IS MOUNTED OUTSIDE `/crm`, ON PURPOSE.
# ...it is called by the platform, after the platform's own
```

The file is in history at **`a6e46fb^:app/routes/internal.py`** and its header
already settled four things, so restoring beats redesigning:

* **Mounted outside `/crm`**, under `/api/v1/internal/`, with the nginx deny
  rule — *"it must never be reachable from a browser — it names an account id
  and acts on it, with no end-user token anywhere in the request."*
* **Fails closed.** An unset secret returns 503 rather than running open:
  *"an internal endpoint that is accidentally public tells you nothing — no
  error, no log line, just an account id anyone can act on."*
* **404, not 403**, for a wrong key, and `hmac.compare_digest`, not `==`.
* **Batched, cap 5,000** — written for the CSV-import case.

⚠️ **For the Brevo model the batch matters less than it did.** A flow step is
one contact at a time, so the lead endpoint is naturally single-record. Keep
the batch shape anyway: the sweep processes list members in pages
(`batch_size`), so a flow activated against an existing 5,000-member list
creates 5,000 deals on the first tick. Same problem the deleted file was built
for, arriving by a different road.

### 3.3 The platform has no caller

`api/services/crm_client.py` **does not exist**. Four files still reference it
in comments (`routes_handler.py:218`, `app/__init__.py:537,547`,
`db/models/registry.py:248`). `crm_service_base_url` and `internal_api_key`
are parsed at `app/__init__.py:554-560` and **read by nothing**.

From the other side, `CRM_API/app/services/auto_link_service.py:23-28`:
*"NO CALLER AS OF PHASE 4a… The only thing that drove this was
`POST /api/v1/internal/crm/auto-link`, which phase 4a deleted along with the
platform hook that called it."*

---

## 4. The decision everything else depends on: whose contact is it?

Brevo does not have this problem — one contact record, one system. We have
two collections after E25: platform `contacts` and CRM-owned `crm_contacts`.
The flow step holds a **platform** contact. What does the deal link to?

### Option A — link the deal to the platform contact (recommended)

Copy nothing. Create the deal and write one association with
`to_type="contact"`.

This already works: `AssociationEntity` carries **both** `contact` (platform)
and `crm_contact` (CRM-owned), and `app/enums.py:46-53` is explicit that
member order is the canonical storage direction. That dual type was built in
the contacts phase for exactly this case. `app/foreign.py` is already the
read-only door onto the host's `contacts` (`contact_by_email:57`,
`contact_by_id:63`, `contacts_by_ids:69`) and `:27` states the rule: *"There
is no `write_*` function here, and there should never be one."*

* **Cost:** nothing is duplicated, so nothing can drift. No migration, no
  second source of truth for an email address.
* **Consequence:** the lead's person has no `lifecycle_stage`, no CRM custom
  fields and no CRM owner until somebody **promotes** it (§6, phase 4) — which
  makes "this is a real prospect now" a visible action instead of a silent
  mirror.
* **Open question:** the deal detail page must render a platform contact
  identically to a CRM one, or the split leaks into the UI.

### Option B — mirror every lead into `crm_contacts`

* **Cost:** a dedupe key, two editable copies of one email address, and the
  `uniq_crm_contact_email` partial index to respect. This is the design that
  produced the backfill you rejected in the contacts phase, for this reason.
* **Gain:** a lead is immediately a full CRM citizen.

### Option C — company only, via the domain linker

`auto_link_service.py` is dead but intact (find-or-create a company from the
email domain). Hang the deal off the company and skip the person.

* **Cost:** a free-email lead gets no company at all — the service's own
  blocklist guarantees it, and that is most self-serve signups.
* This is **half of Option A**, not a rival to it.

**Recommendation: A**, taking the company link from C's existing code.

### 4.1 And the company, specifically

§2.0 blocker B: a typed company name reaches
`Contact.custom_fields["<whatever the author typed>"]` and nowhere else, and
`Contact` has no company field. Three ways out, cheapest first:

| | How | Cost |
|---|---|---|
| **i** | The flow step's config names which custom-field key holds the company (`"company"`, `"company_name"`, …), and the CRM find-or-creates a company from that string | no model change anywhere; the mapping is per-flow and visible. ⚠️ Matching by **name** is weaker than by domain — "Acme", "Acme Ltd" and "ACME" are three companies unless `dedupe`'s normaliser is applied, which exists |
| **ii** | Add a first-class `company` field to the platform `Contact` and a `company` block to both builders | a platform model change plus two builder changes; makes the data reliable for everything, not just deals |
| **iii** | Email domain only, via `auto_link_service.py` | free (dead code, intact) — and gives nothing for a free-email lead, which is most of them |

**Recommendation: i now, ii when forms go to production properly**, and iii as
the fallback when no company field was submitted — they compose: try the typed
name, fall back to the domain, else no company.

---

## 5. Where we follow Brevo, and where we deliberately won't

| | Brevo | Us | Why |
|---|---|---|---|
| Deal created by an automation step | ✅ | **follow** | the whole point of this document |
| Pipeline + stage chosen in the step | ✅ | **follow** | |
| Name defaulted from the contact, with a prefix | ✅ | **follow** | |
| Owner = the contact's owner | ✅ | **follow** | the executor already holds it |
| Optional follow-up task | ✅ | **follow** — later | `crm_tasks` and the reminder scheduler already exist |
| Amount not settable | ✅ | **follow** | an automation cannot know a deal's value |
| No "move stage" action | ✅ | **follow** | create-only keeps the step honest |
| **Re-entry creates another deal** | ✅ | ❌ **diverge** | see below |
| Round-robin via a separate "assign a user" step | ✅ | **follow** | keeps the create step single-purpose |

⚠️ **The one place copying Brevo would be a bug: duplicate deals.** Their docs
say a contact re-entering an automation creates a new deal each time, and
document no dedupe. **We get the better behaviour for free** —
`FlowTriggerLog`'s unique `(flow_id, contact_id)` index means our engine
cannot re-enrol a contact in a flow at all. So one contact, one flow, one
deal, enforced by an index that already exists.

That is worth writing down rather than discovering later, because it means
**no new dedupe table and no new index.** If flow re-entry is ever added to
the engine, this guarantee disappears with it, and that is the moment a
dedupe key is needed — not now.

---

## 6. Phases

### Phase 0 — make website capture actually reach a list (platform-side, and it comes first)

**Nothing downstream can be tested until this is true**, and none of it is CRM
work. In priority order:

**0a. Honour `double_opt_in`, and split "lead" from "subscriber".**
Call `compliance.requires_double_opt_in(enabled=doc.double_opt_in,
source="signup_form")` from the four submit paths — the function and its tests
already exist (`compliance.py:206`, `tests/test_engage_compliance.py:196-204`).
When DOI is off, subscribe and join the list on submit.

⚠️ **Two states, not one.** An enquiry form should be able to create the deal
immediately while leaving the contact `subscribed=False` so marketing cannot
send to it. Collapsing lead and subscriber into one flag is how a quote request
turns into an unconsented newsletter send.

**0b. Refuse to publish a form or page with no list.** Today it is a
browser-side check on forms only (`SignupFormEditor.jsx:661-664`), optional for
landing pages, and `POST /signup-forms/{id}/status` has no check at all
(`content_builder.py:1247-1261`). A list-less form captures leads that trigger
nothing, ever, and says "Thanks for subscribing!" while doing it. Server-side
guard, plus a visible warning on the page editor.

**0c. Fire the trigger on confirm.** Route the confirm handler's raw membership
upsert (`content_builder.py:941-962`) through `ListMembershipCrud.add_contact`
so it picks up the existing `trigger_contact_through_list_flows` call. Removes
the ~2-minute wait, and removes a second copy of the membership-write rule.

**0d. Give a company name somewhere to go.** The decision is §4.1. Without it,
"flow into the pipeline **or company**" can only ever use the email domain, and
most self-serve leads have a free-email address.

**0e. Fix the CORS edges.** An `OPTIONS` handler (or an origin-permissive
middleware branch) for the three JSON onsite endpoints, and CORS headers on the
`/f/{slug}/submit` error responses so a real message reaches the visitor
instead of `alert('Network error.')`. ⚠️ Not on the deal's critical path —
capture works because it posts multipart — but the onsite analytics are dark in
production until it is done.

⚠️ **One thing to settle while in here, even if nothing is done about it:**
the embed's only account binding is `company_id=<raw user ObjectId>` in a
public `<script>` URL (`content_builder.py:2405`), and
`GET /onsite/forms-config?company_id=…` is unauthenticated — so anyone can read
any account's active forms, and post to any known slug. There is no per-account
domain allowlist anywhere. That is a pre-existing property of the feature, not
something this plan introduces, but putting it into production is the moment it
starts mattering.

### Phase 1 — the CRM's door

⚠️ **Phases 1–3 are the MARKETING track.** If the answer to §2.3 is "our
customers post leads from their own backend", the alternative track is
**§2.3 path B — one conditional in `app/auth/context.py` — and it replaces
phases 1, 2 and 3 entirely.** Both tracks end at a deal in a pipeline; they
differ in who initiates. Decide which before starting either, and if the
answer is both, path B is far cheaper and should go first because it is
testable with curl on day one.



Restore `app/routes/internal.py` from `a6e46fb^`, auth half verbatim
(fail-closed secret, `compare_digest`, 404-not-403, nginx deny, mounted
outside `/crm`). Add the lead endpoint beside `auto-link`:

```
POST /api/v1/internal/crm/leads      X-Internal-Key      batch ≤ 5000
```

Body, per contact: the platform `contact_id`, email, name, the account id, the
acting user, and the step's configuration (pipeline, stage, name template,
owner). Returns what it created, per row.

⚠️ **It must never fail the caller's write.** The deleted file already had the
rule: *"a company link that did not happen must not turn into an error on a
request whose real work is done."* Here the caller is a flow step — a CRM
outage must skip the step and record `status: "failed"` in the journey, not
halt the contact's automation.

The standing tax: this router is outside `/crm`, so `check_crm_contract.py`
counts `/crm` only and the documented **95 endpoints is unaffected** — but
`_MOUNT_TABLE`, `EXPECTED_ROUTERS` and `profiles.py` still need the entry, and
a non-literal `include_router` breaks the contract checker's AST read.

Source for the created deal: **`CrmRecordSource.AUTOMATION`**, which exists at
`app/enums.py:144-151` and has never been written by anything. This is what it
was for.

### Phase 2 — the platform's caller

Restore `api/services/crm_client.py`. `crm_service_base_url` and
`internal_api_key` are already parsed at `app/__init__.py:554-560`, so there is
no new configuration. One function: create a deal for a contact.

### Phase 3 — the `crm_deal` step

The fifth step type in `flow_executor.py`, its config model, and its entry in
`FlowBuilder/index.jsx`'s `STEP_PALETTE` with a config panel offering exactly
the fields in §2.2.

⚠️ **Needs the pipelines and stages listed in the builder UI**, which means the
flow builder makes an authenticated call to the CRM — a *browser* call with the
user's own token, not the internal key. That is an ordinary `/crm/pipelines`
read and it needs `pipelines:read`; a user without it cannot configure the step
and the palette entry should say so rather than showing an empty dropdown.

⚠️ **Ownership gate.** This is the phase that needs whoever owns
`flow_executor.py` to agree. Phases 1 and 2 do not, and are useful on their
own — the endpoint is testable with curl, and the client with a one-off
script — so **they should not wait for this conversation.**

### Phase 4 — promote a lead to a CRM contact

The visible half of Option A: a button on a deal whose person is a platform
contact, which creates the `crm_contacts` row and re-points the association
from `to_type="contact"` to `to_type="crm_contact"`.

⚠️ `normalize_pair` owns the canonical direction and `AssociationEntity`
member order defines it — re-pointing is a delete-and-insert through
`_mutate_links`, **never** an in-place `to_type` edit, or the row is stored in
the wrong direction and the unique index stops protecting it.

### Phase 5 (optional) — the follow-up task, and attribution

Brevo's create step can also make a task; `crm_tasks` and
`task_reminder_scheduler.py` already exist, so this is small.

And the attribution tidy-up: four of the twelve inbound routes never stamp
`origin` — `POST /api/v1/contacts`, list-add, the async CSV importer, and the
nevtancloud tracker. ⚠️ **No longer a prerequisite.** In the Brevo model the
deal's provenance is the flow and the list it triggered on, not the contact's
`origin`, so nothing in phases 1–4 depends on this. It is worth doing for
reporting ("which source produces deals that close"), and the earlier draft of
this plan over-ranked it. Backfill is still out: existing rows have no
recoverable origin and guessing one is worse than null.

---

## 7. What we would still not have

| | Why |
|---|---|
| A deal from a form that joins **no list** | `list_join` is the trigger. Phase 0b makes this impossible to configure rather than possible-and-broken; a real `form_submission` trigger is a scheduler change, not a step. |
| A deal before the lead confirms, if DOI stays on | Confirmation is the gate (§2.0 blocker A). Phase 0a makes it a per-form choice; it does not remove the gate. |
| Reliable company data from a website form | Even after §4.1(i), the company is a name typed by a stranger. Domain matching is the better signal and only works for business email. |
| `window.engage.identify()` / `.track()` on a customer site | Blocked by CORS preflight (§2.0 blocker C) until phase 0e. |
| Instant deals, rather than next-tick | The public opt-in path misses the immediate hook (§2.1). Fixable by routing those membership writes through the same helper. |
| A trigger on a deal event (stage changed, won, lost) | That is CRM-417, the other direction. **Brevo does not have this either.** |
| Updating a deal from an automation | Brevo cannot either; their workaround is to put the value on the contact. |
| Inbound email creating a lead | `reply_ingestor.py` only *matches* existing contacts (`_contact_for:193`). No create path. |
| Lead scoring as an entry point | Brevo's leading example. We have no scoring model. |

---

## 8. What I need from you

1. **Does the customer's website have a backend?** (§2.3.) If yes, path B —
   letting an API key authenticate to the CRM, one conditional in
   `app/auth/context.py:127-135` — gets a lead into a named pipeline and stage
   without Landing Pages, Signup Forms, the flow engine, or phases 1–3 of this
   plan. If it is a static site, the key cannot be held safely and the forms
   path (or a serverless proxy) is the only option. **This answer decides how
   much of this document is relevant.**
2. **Should an enquiry form skip double opt-in?** (§2.0 blocker A, phase 0a.)
   My recommendation: yes for CRM intake, no for marketing — the deal is
   created, the contact stays `subscribed=False` until it confirms. This is a
   compliance call as much as a product one, so it is first.
3. **Option A, B or C for identity** (§4), and **i / ii / iii for the company**
   (§4.1). A + i is my recommendation; both reuse things already built.
4. **Who owns `flow_executor.py`** — phase 3 cannot start without that
   agreement, and it is a *smaller* ask than CRM-417, not the same one. Worth
   asking separately rather than bundling.
5. **Should a lead with no company still create a deal?** A free-email lead
   gets no company from either the domain or a typed field. Deal with a person
   and no company, or no deal?
6. **Confirm phases 0, 1 and 2 may start now**, ahead of that conversation —
   phase 0 is `emailMarketingAPI` and `emailMarketingUI`, phases 1 and 2 are
   split across the platform and the CRM. All inside code we own.

And the standing one, now corrected: the **contacts restore is committed**
(`CRM_API` at `a6e46fb`, `emailMarketingUI` at `47a1596`) — I had been saying
otherwise. What is uncommitted is this session's output: the reports module,
the Reports screen, the un-hidden filter bar, and these plan documents. The
full inventory is in `docs/remaining-work.md`.
