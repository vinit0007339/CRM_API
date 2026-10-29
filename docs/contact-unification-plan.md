# One contact for marketing and CRM

**Decided 29 Oct 2026.** The CRM stops owning a second population of people.
`contacts` becomes the single person record, shared with marketing exactly as
Brevo shares it, and the per-account CRM data that cannot live on a shared
document moves to a sidecar keyed `(account, contact_id)`.

This reverses `aee58ca`, which severed the two. That severance was the right
call *at the time* and for a reason that still holds — the reason is addressed
below rather than waved away.

---

## 1. Why not the other two options

**Make `contacts` per-account** (`email` unique on `(created_by, email)`, fold
`crm_contacts` in). This is what Brevo actually does and it is the cleanest end
state. Rejected for now because `email: Indexed(unique=True)` is *global*, and
`owner_ids` means one document is deliberately **shared** between accounts. The
migration would have to split every shared document into one row per owner, and
every marketing lookup-by-email would have to become account-scoped in the same
change. `contact_scope.py` already documents the paths that cannot be scoped
while the unique index stands — the create-if-missing ones — so this is not a
reversible afternoon.

**One contact in the UI only.** Cheap, but it keeps two records and therefore
keeps CRM-406: an association row whose id could belong to either collection.

**The sidecar** keeps one person, one id space, and no index change.

---

## 2. What lives where

The split is not arbitrary: **a fact about the person** goes on the contact, **a
fact about this account's relationship with them** goes on the profile.

| Field | Lives on | Why |
|---|---|---|
| email, first_name, last_name, phone | `contacts` | the person |
| subscribed, sms_consent, subscribe_date, list_ids, events, geo, device | `contacts` | marketing state, already there |
| job_title, linkedin_url | `contacts` *(new, additive)* | describes the person, not the relationship |
| owner_id | `crm_contact_profiles` | two accounts holding the same person have different owners |
| lifecycle_stage | `crm_contact_profiles` | a lead to us may be a customer to them |
| primary_company_id / _name | `crm_contact_profiles` | per-account company graph |
| tag_ids | `crm_contact_profiles` | tags are per-account |
| custom_fields (CRM attributes) | `crm_contact_profiles` | **see the collision below** |
| description, source | `crm_contact_profiles` | per-account annotation |
| deal_count, open_deal_value, open/overdue_task_count, last_activity_at | `crm_contact_profiles` | rollups over this account's records |
| is_archived | `crm_contact_profiles` | archiving is per-account |

⚠️ **`custom_fields` would have collided, and that alone justifies the
sidecar.** `Contact.custom_fields` already exists and holds zero-party form
data keyed by *form field name*. CRM attribute values are keyed by
*`internal_name`* and are defined per account in `crm_attributes`. Merging the
two namespaces onto one shared document would let one account's attribute
silently overwrite another's form answer. They stay separate dicts in separate
collections.

### The profile

```
crm_contact_profiles
  created_by        account tenancy, leading index key
  contact_id        -> contacts._id
  owner_id, created_by_user_id
  lifecycle_stage, description, source
  primary_company_id, primary_company_name
  tag_ids[], custom_fields{}
  deal_count, open_deal_value, open_task_count, overdue_task_count
  last_activity_at, is_archived, created_at, updated_at

  UNIQUE (created_by, contact_id)      <- one profile per account per person
  (created_by, is_archived, ...)       <- list
  (created_by, owner_id)               <- "my contacts"
  (created_by, primary_company_id)     <- company page
```

A contact with no profile in this account is simply a marketing contact that
nobody has worked yet. The CRM list creates the profile lazily on first edit,
so the marketing audience does not silently become a CRM backlog.

---

## 3. One id space

`crm_associations`, `crm_tasks.contact_ids` and `crm_notes.contact_ids` all
currently hold **`crm_contacts` ids**. After this they hold **`contacts` ids**,
and `AssociationEntity.CRM_CONTACT` stops being written.

The enum member **stays**: its position defines the canonical storage direction
and removing it would orphan every existing row. It becomes legacy — read, never
written — and the migration rewrites the rows that use it.

This is what closes CRM-406. There is no longer a second id space for a row's
`to_id` to belong to, so nothing has to guess.

---

## 4. Migration

`scripts/migrate_contact_unification.py`, idempotent, resumable, dry-run first.

1. **Map.** For each `crm_contacts` row, resolve its platform contact:
   `platform_contact_id` if set, else account-scoped lookup by email, else a
   global lookup (the unique index forces this — see `contact_scope`), else
   create. Record `crm_contact_id -> contact_id`.
2. **Profile.** Upsert one `crm_contact_profiles` row per `(account,
   contact_id)` carrying the CRM fields.
3. **Associations.** Rewrite `to_type="crm_contact"` rows to `"contact"` with
   the mapped id. ⚠️ **Dedupe before writing** — a deal linked to *both* the
   platform contact and the CRM contact for the same person collapses to one
   pair and would otherwise violate `uniq_crm_association_pair`.
4. **Leaf arrays.** Remap `crm_tasks.contact_ids` and `crm_notes.contact_ids`.
5. **Leave `crm_contacts` in place**, read-only, as the rollback path. Drop it
   in a later release once the new path has run clean.

### Known hazards

- ⚠️ **Phone-only leads have no email.** `Contact.email` is `Optional` *and*
  uniquely indexed, which is the exact `sparse`-vs-`partial` trap
  `models/contact.py` already documents on the CRM side. Two e-mail-less
  contacts may collide on insert. **Verify the live index before step 1** and
  make it partial if it is not.
- A `crm_contacts` row whose email belongs to **another account's** contact
  document: the person is shared, which is the model working as intended. The
  profile is still this account's own.
- Deleting a contact must not cascade: removing a profile leaves the marketing
  contact, and vice versa.

---

## 5. Phases

| | What | Repo | Reversible |
|---|---|---|---|
| **P1** | `CrmContactProfileModel` + `CrmContactProfileCrud`, registered, nothing reads them yet | CRM_API | ✅ done |
| **P2** | `job_title` / `linkedin_url` on the platform `Contact` | platform | yes, additive |
| **P3** | Read path: contact list + detail serve `contacts` joined to the profile; `linked_deals` / `linked_companies` keep working | CRM_API | behind a flag |
| **P4** | Write path: create/edit writes the contact and upserts the profile; lazy profile creation | CRM_API | behind a flag |
| **P5** | Associations, tasks and notes write `contact` instead of `crm_contact` | CRM_API | needs P6 |
| **P6** | The migration script, dry-run then apply | CRM_API | rollback = `crm_contacts` |
| **P7** | UI: one Contacts screen, with marketing state and campaign engagement on the record | UI | yes |
| **P8** | Retire `crm_contacts` and the `promote` endpoint | both | final |

P1–P2 are additive and safe to land immediately. **P3 onward should sit behind
a per-account flag** so one account can be cut over and watched before the rest.

---

## 6. Decisions — settled 29 Oct 2026

| | Question | Decision |
|---|---|---|
| 6.1 | Flag scope | **Per-account `crm_settings.unified_contacts`, plus a service-wide env kill switch.** Cut one account over and watch; revert everybody in one deploy if it goes wrong. |
| 6.2 | What the CRM list shows | **Worked contacts by default, with an explicit "All contacts" view.** Keeps owner/lifecycle sort and filter index-served, and still reaches the whole audience when somebody needs to find a person to work. |
| 6.3 | `POST /crm/contacts/promote` | **Removed in phase 5**, not deprecated. See the coupling note below. |
| 6.4 | Deleting from the CRM | **Archives the profile only.** Never touches subscription or list membership. Unsubscribing stays a separate action in a separate place. |

⚠️ **6.3 pulls UI work into phase 5.** `promoteContact` is called from
`emailMarketingUI/src/screens/CRM/Deals/DealDetails.jsx:855` — the Deal page,
not only the Contacts screen. Deleting the endpoint without that call site
going in the same release leaves a button that 404s on a page nobody was
touching. Phase 5 therefore ships with a small UI change, and
`EXPECTED_ENDPOINTS` drops 97 → 96 with `docs/api-contract.md --emit`
regenerated there rather than in phase 9.
