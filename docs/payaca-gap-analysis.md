# Payaca CRM — gap analysis against ours

**Plan only. Nothing implemented.**

Source: `https://payaca.com/uk/features/crm`, read 27 Oct 2026. Our side is
measured, not remembered — every "we have" below names where it lives.

---

## ⚠️ DECIDED — read this before §2

The four questions this document ended on have been answered, and the scope was
narrowed afterwards to **pipeline, company, deals and the customer flow**. The
analysis below is unchanged and still accurate as analysis; what changed is
which of it is going to be built. `payaca-phases.md` is the live plan.

| Question | Answer | Effect below |
|---|---|---|
| Quoting / invoicing in scope? | **No, never** | §3's first two rows are permanent, not assumed |
| Who owns `flow_scheduler.py`? | **Nobody** | §2.2 is **parked**, not pending — Epic 30 cannot start |
| @mentions notification path | **Dropped** | §2.4 is cut |
| Communications: automatic or pinned? | moot | §2.1 is **out of the narrowed scope** |

So of the four gaps in §2, exactly one survives: **§2.3, pipeline reporting.**
It is Phase 2 of `payaca-phases.md`. §2.1 and §2.2 are kept here because both
are cheaper than they look and the measurements will be wanted when they
return; §2.4 is kept only so the reasoning is not rediscovered.

---

## 0. The thing to settle first

**Payaca is not a sales CRM. It is field-service business management for
trades** — solar, heat pumps, installations. Its CRM is the customer layer
under job management: quoting, scheduling, invoicing, DNO applications,
compliance docs, a field mobile app, map-based dispatch.

So most of that page is not a gap in our CRM; it is a different product. Copying
it feature-for-feature would build a trades platform inside an email marketing
tool. What *is* worth taking is the handful of things Payaca does that any CRM
needs and ours does not do — and there are four of them, one of which we are
unusually well placed to build.

A second observation that matters more than any single feature: Payaca's CRM is
wrapped around **money and messages**. A customer record shows what is
invoiced, awaiting payment and paid; a project shows every email sent and
received about it. Ours shows pipeline value and internal field changes. That
difference, not the feature count, is the real distance.

---

## 1. What we have, measured

91 endpoints across 13 routers, 15 collections.

| Module | Endpoints | |
|---|---|---|
| companies | 16 | records, links, merge, dedupe, bulk, export |
| deals | 15 | + Kanban board router, close/reopen, forecast |
| pipelines | 11 | stages, reorder, delete-with-reassign |
| contacts | 8 | CRM-owned people, `lifecycle_stage`, bulk, activities |
| tasks | 7 | + task-types |
| notes | 6 | on company / deal / contact |
| imports | 6 | CSV with mapping, dry run, error report |
| files | 5 | attachments, signed download |
| attributes | 5 | 13 custom field types, per object |
| saved-views | 4 | shared filter sets |
| view-preferences | 3 | per-user column layouts |
| settings | 2 | currency, industries, lost reasons, task types, card display |
| exports | 2 | company + deal CSV |

Also present and relevant to the comparison:

- **Activity timeline** — `activity_recorder` emits created, updated (with a
  field-level diff), stage_changed, closed, reopened, owner_changed, linked,
  and note events, on company, deal and contact.
- **Weighted forecasting** — `forecast.py`: per-deal probability, weighted
  amounts, per-stage and per-column weighted totals.
- **Rollups on the record** — `deal_count`, `open_deal_value`,
  `open_task_count`, `overdue_task_count`, `last_activity_at`.
- **Tags on all three objects** — `tag_ids` on company, deal and contact.
- **Auto-link by email domain** — a contact's company is created or matched
  from its address, with a free-email blocklist.
- **Merge and dedupe** — `merge_service`, `dedupe` with confidence scoring.
- **RBAC** — 32 permissions, plus `crm:manage_others` for cross-owner edits.

### Where we already match Payaca and it is worth saying so

**"Projects under customers, each with its own pipeline, stage, tags and
owner."** That is our company → deals relationship exactly: many deals per
company, each with pipeline, stage, owner and tags. No gap. Do not let the
different noun suggest otherwise.

**Drag-and-drop pipeline, values at each stage.** We have the Kanban board with
a dedicated router and per-stage weighted values.

**Tags, custom fields, project history.** All present; custom fields are richer
(13 typed attribute types, per object type, with typed-column routing).

---

## 2. The gaps that matter

Four, ranked by value-to-effort rather than by how prominent they are on
Payaca's page.

### 2.1 Communications are not logged against the record — OUT OF THE NARROWED SCOPE

**Payaca:** every customer email tracked against the right project, via a
per-user forward address and BCC address. Search matches sender, recipient, CC
and the original sender of a forwarded thread. Sending a proposal or invoice
lets the user pick which verified address it goes from, remembered per user.

**We have:** manual notes, and an activity timeline of *internal field
changes*. Not one email appears on a company, deal or contact.

**Why this is the sharpest gap:** we are an email marketing platform. The
record that cannot show you an email is the one in the product whose entire job
is email. A salesperson opening a deal sees who changed its stage and nothing
about the eleven messages that moved it.

**Why it is cheaper for us than for Payaca:** the ingestion already exists.
`emailMarketingAPI/api/routes/inbox.py` stores inbound customer replies as
threads, per account, with `sanitized_html` cleaned at write time, behind
`inbox:read` / `inbox:write`. Campaign sends are already recorded. So this is
mostly a **join and a surface**, not a pipeline:

1. resolve an inbox thread's participant to a CRM contact by email (scoped —
   `contact_scope`'s rules apply), and from the contact to its company and open
   deals through `crm_associations`;
2. add a read-only "Communications" tab to the company, deal and contact detail
   pages, paging over threads and campaign sends;
3. let a note or thread be pinned to a specific deal when the automatic
   resolution is ambiguous — a contact with three open deals needs a human to
   say which one.

Deliberately **not** in scope for a first cut: the forward/BCC capture address,
and sending from the CRM. Both are real features; neither is needed to make the
record stop lying about what it knows.

**Size:** medium. The resolution step is the only hard part, and it is the same
email-to-contact scoping problem the note importer already solves.

### 2.2 No automations — specced, and now PARKED (no engine owner)

**Payaca:** per-stage automations; triggers across projects, proposals,
invoices, events and documents; actions that send email and SMS follow-ups,
move stages, create tasks and apply tags.

**We have:** nothing emitting, and no consumer. But **`docs/CRM/crm-event-contract.md`
(CRM-417) is written and awaiting sign-off**, and it is thorough: a payload
shape, six events (`crm.deal.created`, `.stage_changed`, `.won`, `.lost`,
`.reopened`, `crm.company.created` / `crm.contact.created`), a delivery
guarantee using `crm_activities` as the outbox, tenancy, ordering, and the rule
that emission must neither fail the write nor be silent.

Critically, §3.1–§3.6 oblige the **workflow engine**, not the CRM, and the
document names §3.2 as a blocker: `(flow_id, contact_id)` cannot express a CRM
event, because a deal event is not about a contact. The platform already has
`flow_scheduler.py` and `custom_flows`, so the actions Payaca lists — send an
email, send an SMS, create a task, apply a tag — mostly exist on the other side
of that seam.

**So the gap was never "build automations". It was "get CRM-417 co-signed".**

⚠️ **AND IT CANNOT BE: nobody owns `flow_scheduler.py` or `custom_flows`.**
With no co-signer there is no signature, so Epic 30 (CRM-418/419/420) is
**parked, not pending** — the distinction matters, because "pending" invites
someone to start CRM-418 against an unagreed contract and §3.2 says the engine's
data model cannot carry a CRM event at all.

What this leaves: the CRM's own half is small and stays unbuilt. Nothing here
decays while it waits — `crm_activities` keeps accumulating the rows the outbox
design needs, so the emitter is no more expensive in six months than today. If
an owner appears, this becomes the highest-leverage item on the list again.

**Size:** the CRM's half is small — an emitter on six paths and the outbox
already designed. The engine's half is the real work and is not ours.

### 2.3 No conversion or pipeline-health reporting

**Payaca:** conversion rates tracked across the team; pipeline health.

**We have:** `forecast.py` answers *"what is the pipeline worth"* — weighted by
stage probability. It does not answer *"where do deals die"*, *"how long do they
sit in each stage"*, or *"who converts"*.

**What is missing specifically:** stage-to-stage conversion and drop-off, time
in stage, win rate by owner and by pipeline, and lost-reason distribution. The
last one is nearly free: `crm_settings.lost_reasons` already exists and
`crm.deal.lost` already carries `lost_reason_id`, so the data is being
collected and nothing reads it back.

**Size:** small-to-medium, and it is aggregation over data we already have —
`crm_activities` holds `stage_changed` with timestamps, which is exactly the
series a time-in-stage report needs. No new writes.

### 2.4 No @mentions in notes — DROPPED

**Payaca:** team members tag colleagues in customer notes.

**We have:** notes with a body and an author, no mentions and no notification
path.

**Size:** small on its own, with one caveat that made it bigger than it looked —
a mention is only useful if it notifies, and measured, there is **no
notification route and no notification model**, and the `push` service's stage
config is incomplete (it warns at boot that `push_stage_project_id` is missing
and sends will be refused).

**Decided: dropped.** It was the smallest of the four gaps and the only one
whose value depended on infrastructure that does not exist. Building a
notifications surface to carry it would have been the real project, and nothing
else was asking for one.

---

## 3. Deliberately out of scope

Named so the decision is recorded rather than re-litigated:

| Payaca feature | Why not |
|---|---|
| Invoicing, payment collection, customer portals | Engage bills **our** customers; it does not help them bill theirs. A different product. |
| Customer value split into to-invoice / awaiting-payment / paid | Follows from invoicing. Without it the split has no source data. |
| Map-based scheduling, field service mobile app | Trades dispatch. Nothing in Engage's audience needs it. |
| Interactive proposals, solar design (Glow), heat pump estimates (Flow) | Vertical-specific sales tooling. |
| DNO / grid connection applications, compliance workflows, service plans | UK trades regulatory. |
| HubSpot migration tooling | Our CSV importer (6 endpoints, mapping, dry run, error report) covers the need. Revisit only if a real migration is lost to it. |

### Agent Dave (AI) — a maybe, and not now

Natural-language questions over account data, building automations, editing
templates, asking before it changes anything, working within the user's
permissions.

The valuable half is the **read-only** half: *"which deals have stalled"*,
*"what is unpaid"*. That is a query layer over data we already hold, and it
respects RBAC naturally because it can run as the user.

The half that writes — building automations — must come **after** §2.2, because
there is nothing to build automations out of until the event contract lands.

Worth revisiting once §2.1 and §2.2 are done, not before. Listed here so it is
not mistaken for an oversight.

---

## 4. Suggested order — SUPERSEDED

⚠️ Kept as the record of how the order was reasoned about. The live plan is
`payaca-phases.md`, which walks the customer flow instead and reaches a
different answer: of the four items below, (1) cannot happen, (3) is out of
scope and (4) depends on (1), so only (2) survives.

### The original order

Not phases with dependencies this time — four independent pieces, ordered by
what each unblocks.

| | Work | Why here |
|---|---|---|
| **1** | **Get CRM-417 co-signed.** Not code: a decision from whoever owns `flow_scheduler.py` and `custom_flows` on §3.1–§3.6. | Unblocks Epic 30 entirely. A conversation, and it is already waiting. |
| **2** | **Lost-reason and conversion reporting.** Stage-to-stage drop-off, time in stage, win rate by owner, lost-reason distribution. | Smallest real gap, no new writes, reads `crm_activities` and settings we already populate. Answers a question every pipeline owner asks. |
| **3** | **Communications on the record.** Resolve inbox threads and campaign sends to CRM records; a read-only tab on company, deal and contact. | The gap that most changes what the product feels like, and the one our architecture makes cheap. |
| **4** | **The CRM emitter** for the six CRM-417 events, once (1) is signed. | Small on our side; the engine's half is not ours to schedule. |

@mentions sits outside this order: do it when the notification question is
answered, not before.

---

## 5. What I needed from you — ANSWERED

All four are answered in the banner at the top of this document. Kept so the
questions and their answers sit together.

### The original questions

1. **Is invoicing/quoting ever in scope for Engage?** → **No, never.** §3's
   first two rows are settled permanently and the "customer value" comparison
   stops being raised.

2. **Who owns `flow_scheduler.py`?** CRM-417 has been written and waiting since
   22 Oct. It needs one person's agreement on six sections, and nothing in Epic
   30 can start without it.

3. **Is there any notification surface for the CRM**, in-app or email? The
   answer decides whether @mentions is a small feature or a half one.

4. **For communications: automatic resolution only, or pinning too?** A contact
   with three open deals cannot be resolved automatically. Pinning is more work
   and the feature is noticeably weaker without it.

---

## 6. One honest note on sequencing

Everything above assumes the CRM contacts work now in flight is finished first.
As of this document it is not: the restore is uncommitted, `crm_contacts` is
live but empty, no backfill was run, and the stale
`uniq_crm_contact_email` index is still on at least one local database. None of
the four gaps here is worth starting while that is true — §2.1 in particular
resolves emails to CRM contacts, which means it depends on contacts being
settled.
