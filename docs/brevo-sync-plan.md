# Syncing our CRM with the Brevo workflow spec

Measured against `brevo_crm_workflow_instructions.md` (rev. 28 Oct 2026) and the
three repos as they stand on **28 Oct 2026**.

**Headline:** the spec is behind the code, not ahead of it. On object model,
tenancy, associations, stage rules, activity history and extensibility we are
already past what the document describes — in several places the document's
design is the weaker one. There are **six real gaps**, one of which is a
deliberate architectural decision rather than missing work, and the cheapest
item on the list is a single commented-out line of navigation.

This plan is scoped to Brevo parity only. It does **not** restate
`remaining-work.md` (groups A–D) or `lead-intake-plan.md`; where the two
overlap it points at them.

---

## 1. Where we already exceed the spec

Do not re-implement any of this. Listed because the spec's §12 schema would be
a downgrade if taken literally.

| Spec says | We have | Verdict |
|---|---|---|
| Three separate M2M tables (`company_contacts`, `deal_contacts`, `deal_companies`) | One `crm_associations` collection with a canonical direction (company < deal < contact), `uniq_crm_association_pair`, and reverse index | Ours is better — removes the PATCH-replaces-the-array bug class the spec inherits from Brevo |
| `pipeline_stages` rows with `is_closed` / `is_won` | Embedded `PipelineStage` with `StageType`, `win_probability`, `required_attributes`, `rotting_days`, fractional `position` | Ours is better — one read per board, no orphan stages |
| `deals.status` kept in step with stage flags | `DealModel.status` denormalised from stage type, written in the same op | Parity |
| `deal_stage_history` table | `crm_activities` append-only timeline + `reporting.transitions_by_deal` + `/reports/time-in-stage` | Parity, achieved differently. **No separate collection is needed** |
| Stage ageing from `stage_entered_at` | `DealModel.stage_entered_at`, reset on every stage change; drives rotting too | Parity |
| `account_id` tenancy, timestamps, soft delete | `created_by` as leading index key everywhere; `is_archived`; deals deliberately exempt (documented in `deal.py`) | Parity |
| `saved_views` | `CrmSavedViewModel`, storing the filter *group* not a compiled Mongo fragment | Ours is better |
| Custom objects as a future subsystem | `crm_attributes` descriptor + hybrid typed/`custom_fields` already shipped | Ahead |
| — | Merge/unmerge with reversible snapshot, domain + name-normalised dedupe, bulk actions, import/export workers, files, notes, weighted forecast, task reminder scheduler with claim-based locking | Not in the spec at all |

**Action:** amend the spec's §12 to record that the SQL sketch was superseded,
and that `crm_associations` and the embedded-stage model are the house pattern.
One paragraph, not a rewrite.

---

## 2. The six gaps

### G1 — Marketing↔CRM severance *(decision, not defect)*

The spec's §3, §8 and §12 assume **one contact** that carries CRM fields, list
membership, subscription state and campaign engagement side by side. Our CRM
contact carries none of them, on purpose — `app/models/contact.py` states it
plainly: *"a CRM contact is not marketable."* The platform's `contacts.email`
is globally unique across accounts and cannot host per-account CRM fields; the
seam produced two populations of the same person with no authoritative rule,
and that ambiguity reached the association collection (CRM-406).

So the spec and the code disagree on the central object. That has to be
resolved in the spec, not reopened in the code.

**Three options, in preference order:**

1. **Amend the spec** (recommended). Rewrite §3's engagement bullet, §8 and
   §12's `email_events` / `lists` / `list_contacts` / `segments` tables as
   *platform-side* concerns, and state the severance with its reasoning. Cost:
   one editing pass. Changes no code.
2. **Read-only engagement panel.** Where `platform_contact_id` is set (i.e. the
   contact was promoted), show sent/delivered/opened/clicked and the last few
   events on the contact detail page, read-only, clearly labelled as marketing
   data. Satisfies the spirit of §3 without re-linking anything. Cost: one
   internal read endpoint + one panel. **Blocked on nothing.**
3. **Re-link.** Rejected — reopens CRM-406.

### G2 — Conversations do not reach CRM timelines

Spec §7 puts an inbox thread on the contact, company **and** deal timeline.
`screens/Inbox` exists; `CrmActivityModel` has the shape to carry it
(`activity_type`, `source: ActivitySource`); nothing writes conversation
events into it. Lower value than G1-option-2 and in the same family — defer
until G1 is settled, then do both through one internal read path.

### G3 — Tasks screen is built and unreachable

`src/constants/menuConfig.js:154` is commented out. The screen
(`screens/CRM/Tasks`), seven API endpoints, reminders with claim-based
scheduling, and the per-record task panels are all shipped and working.

**This is the cheapest item in the document: one line.** It needs the
`crm_tasks:read` permission confirmed on the roles you ship, and a browser
pass. Spec §7's All/Overdue/Today/This week buckets should be checked against
what the screen renders while you are in there.

`crm-fields` (line 152) is commented out the same way — same question, same
cost.

### G4 — Reports: three of the spec's KPIs are missing

We have `/reports/pipeline` (which already returns `win_rate_by_owner`),
`/conversion`, `/time-in-stage`, `/lost-reasons`. Against spec §11:

| Spec KPI | Status |
|---|---|
| Open deals count + value per pipeline/stage | ✅ |
| Weighted pipeline value | ✅ `forecast.py` |
| Won revenue / lost value | ✅ |
| Win rate | ✅ in `/pipeline` |
| Stage ageing | ✅ `/time-in-stage` |
| Stage-to-stage conversion | ✅ `/conversion` |
| Lost reasons | ✅ (not even in the spec) |
| **Sales cycle duration** (opened → won, median + mean) | ❌ |
| **Deals expected to close by date** | ❌ |
| **Overdue tasks / follow-up completion rate** | ❌ |

All three are aggregations over data we already store. Sales cycle needs
`created_at` → `actual_close_date` on won deals; close-by-date needs
`expected_close_date`, already indexed; task compliance needs `crm_tasks`
grouped by `is_done` / `due_at` / `assigned_to_id`.

### G5 — Saved views and filter bar not on Contacts

`saved_view_rules.OBJECT_TYPES` already includes `contact`; the backend is
ready. `remaining-work.md` group B un-hid the bar on Companies and Deals list
view only. Contacts has `/filter-fields` but no bar.

### G6 — Automation on deal-stage change

Spec §10's second example. This is **C9** in `remaining-work.md` — the
`crm_deal` flow step — and is the one item blocked on someone else's agreement
(`flow_executor.py` ownership, A6.4). No new analysis here; the spec's warning
that a stage-change automation *"may execute once per associated contact"*
should be carried into C9's acceptance criteria verbatim, because our deals are
genuinely many-to-many with contacts and will hit it.

---

## 3. Explicitly not doing

- **Custom objects** — spec puts them last, correctly. `crm_attributes` covers
  the real demand.
- **Open-deal plan quota** — Brevo's "1/50 open deals" is their commercial
  packaging, not a model requirement.
- **A separate `deal_stage_history` collection** — `crm_activities` already
  serves it and the time-in-stage report proves it.
- **Spec §14's observed Crescent Homes config** — reference environment for the
  doc, not a target for our defaults. Ours is INR / Asia/Kolkata by design.

---

## 4. The plan

Ordered by cost-to-value, not by spec section number. P0 is gated on
`remaining-work.md` **A1** (the `uniq_crm_contact_email` index) and **A2**
(commit group B) — nothing below is testable end to end until A1 runs.

| | Work | Repo | Size | Depends on |
|---|---|---|---|---|
| **P1** | Uncomment `crm-tasks` in `menuConfig.js`, confirm `crm_tasks:read` on shipped roles, browser pass on the four due buckets | UI | **1 line + 15 min** | A1, A2 |
| **P2** | Same for `crm-fields` (line 152) — decide ship or delete | UI | 1 line | — |
| **P3** | Amend the spec: §12 superseded-by note, §3/§8/§12 marketing severance stated with its reasoning, §6 stage names already corrected | doc | 1 pass | — |
| **P4** | Filter bar + saved views on Contacts list, mirroring Companies | UI | small | A4 |
| **P5** | `/reports/sales-cycle` — median and mean days opened→won, by owner and by pipeline | CRM_API | small | — |
| **P6** | `/reports/closing` — open deals grouped by expected close window, with weighted value | CRM_API | small | — |
| **P7** | `/reports/task-compliance` — open, overdue, completed-on-time by assignee | CRM_API | small | — |
| **P8** | Reports screen: three new panels for P5–P7 | UI | medium | P5–P7 |
| **P9** | Read-only marketing engagement panel on contacts with `platform_contact_id` (G1 option 2) | CRM_API internal read + UI | medium | P3 decision |
| **P10** | Conversation threads into `crm_activities` | both | medium | P9 |
| **P11** | `crm_deal` flow step — carry the once-per-contact warning into acceptance | platform + UI | medium | **A6.4**, C8 |

**P1–P3 are a single afternoon** and close the three gaps a user would notice
first. P5–P8 are the only genuine feature build in the list and need nothing
from anyone else. P9–P11 should not start until P3 settles what the spec says
about the marketing seam.

---

## 5. Open questions

1. **G1** — amend the spec (option 1) or also build the read-only panel
   (option 2)? Everything from P9 down hangs on this.
2. **G3** — were `crm-tasks` and `crm-fields` commented out for a reason that
   is still true, or left over from the phased rollout?
3. **P5** — sales cycle measured from `created_at` or from first entry into the
   first open stage? They differ for imported deals.
4. **P6** — which close windows? Overdue / this month / next month / later, or
   calendar quarters?
