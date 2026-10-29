"""CrmContactProfileModel — this account's relationship with a person.

Phase 1 of `docs/contact-unification-plan.md`. **Nothing reads this yet**; it
is registered so the collection and its indexes exist before the read path
lands behind a flag.

⚠️ **THE PERSON IS NOT HERE.** Name, email, phone and every marketing fact
live on the platform `contacts` document, which is the single person record.
This collection holds only what is true of *this account's* relationship with
them — owner, lifecycle, the company they sit at in our graph, our tags, our
attribute values, our rollups. The split rule is one sentence: a fact about
the PERSON goes on the contact, a fact about the RELATIONSHIP goes here.

⚠️ **AND THAT SPLIT IS FORCED, NOT STYLISTIC.** `Contact.email` is
`Indexed(unique=True)` — globally unique across every account — and
`owner_ids` lets one document be deliberately shared between accounts. So two
accounts can legitimately hold the same person, and a per-account field on
that shared document has no answer to "whose". `owner_id`, `lifecycle_stage`
and `custom_fields` are exactly those fields, which is what `models/contact.py`
gave as the reason for a second collection in the first place. The sidecar
answers it without a second population of people: one person, many
relationships, keyed `(created_by, contact_id)`.

⚠️ **`custom_fields` HERE IS NOT `Contact.custom_fields`, AND THEY MUST NOT BE
MERGED.** The platform's dict holds zero-party form data keyed by *form field
name*; this one holds CRM attribute values keyed by *`internal_name`* from
this account's `crm_attributes`. One shared namespace would let one account's
attribute silently overwrite another account's form answer. Two dicts, two
collections, no collision.

⚠️ **ABSENCE IS MEANINGFUL.** A contact with no profile in this account is a
marketing contact nobody has worked yet — not an error and not a deleted
record. Profiles are created lazily on first CRM edit, so a 400,000-subscriber
audience does not become a 400,000-row CRM backlog on the day this ships.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class CrmContactProfileModel(Document):
    # ── tenancy ──────────────────────────────────────────────────────────────
    created_by: PyObjectId                      # the account
    created_by_user_id: Optional[PyObjectId] = None   # the human

    #: The person, in `contacts`. THE ONE ID SPACE — `crm_associations`,
    #: `crm_tasks.contact_ids` and `crm_notes.contact_ids` all point at this
    #: same id once phase 5 lands, which is what closes CRM-406: a row's
    #: `to_id` can no longer belong to either of two collections.
    contact_id: PyObjectId

    # ── identity cache ───────────────────────────────────────────────────────
    #
    # ⚠️ A CACHE OF THE PERSON, AND THE ONLY REASON IT EXISTS IS THAT YOU
    # CANNOT SORT ACROSS TWO COLLECTIONS. The contact list drives from this
    # collection (plan decision 0.2), sorts by name ascending, and searches
    # name, email and company. Those first two are facts about the PERSON and
    # live on the platform `contacts` document — so without a copy here the
    # default list could not order or search itself.
    #
    # ⚠️ AND A `$lookup` IS NOT THE ALTERNATIVE. `contacts` is reached through
    # the `Directory` interface in `app/host/directory.py`, which has three
    # implementations — one shared database, one HTTP call, one none. A join
    # only exists in the first. Anything this list needs to sort or filter on
    # therefore has to be local.
    #
    # ⚠️ THESE THREE AND NO MORE. The set is exactly what
    # `queries.profile_sort` orders by and `profile_filter`'s `q` searches —
    # not "useful person fields". Every addition is another field to drift,
    # and display-only values are read live from the contact instead.
    #
    # ⚠️ DRIFT IS REAL AND IS HANDLED OUT OF BAND. Marketing can rename or
    # re-address a contact and nothing tells the CRM, because the platform does
    # not know this collection exists. `scripts/reconcile_contact_profiles.py`
    # repairs them in batches through the existing `contacts_by_ids` door. A
    # stale name here is a wrong label and a missed search hit — never a wrong
    # record, because every id in this document is still authoritative.
    full_name: Optional[str] = None
    email: Optional[str] = None
    job_title: Optional[str] = None

    # ── the relationship ─────────────────────────────────────────────────────
    owner_id: Optional[PyObjectId] = None
    lifecycle_stage: Optional[str] = None       # see contact_rules.LIFECYCLE
    description: Optional[str] = None
    source: Optional[str] = None                # CrmRecordSource value

    primary_company_id: Optional[PyObjectId] = None
    #: Denormalised, same trade-off as `DealModel.primary_company_name`: the
    #: list renders it on every row and `CompanyCrud.rename_fanout` keeps it
    #: current.
    primary_company_name: Optional[str] = None

    tag_ids: List[PyObjectId] = Field(default_factory=list)
    #: CRM attribute values. See the warning in this module's docstring.
    custom_fields: Dict[str, Any] = Field(default_factory=dict)

    # ── rollups ──────────────────────────────────────────────────────────────
    # Display only, maintained by $inc from the write paths and reconciled by
    # a nightly job — the same contract the company and deal rollups carry, so
    # drift can never change behaviour.
    deal_count: int = 0
    open_deal_value: float = 0.0
    open_task_count: int = 0
    overdue_task_count: int = 0
    last_activity_at: Optional[datetime] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    #: Archiving is per-account and NEVER touches the person: archiving a
    #: profile must not unsubscribe anybody, and unsubscribing must not archive
    #: a profile. Two separate actions on two separate records.
    is_archived: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_contact_profiles"
        indexes = [
            # ⚠️ ONE PROFILE PER ACCOUNT PER PERSON. Not partial and not
            # sparse: unlike `crm_contacts.email`, both keys are always
            # present, so a plain compound unique says exactly what is meant.
            # It is also the lazy-create guard — two concurrent edits of the
            # same contact race to upsert, and this is what makes the loser a
            # duplicate-key to catch rather than a second profile nobody sees.
            IndexModel(
                [("created_by", ASCENDING), ("contact_id", ASCENDING)],
                unique=True,
                name="uniq_crm_contact_profile",
            ),
            # The CRM contact list's DEFAULT ORDER — name ascending, the
            # same shape `crm_contact_account_name` served on the collection
            # this replaces. Without it the default view sorts in memory.
            IndexModel(
                [("created_by", ASCENDING), ("is_archived", ASCENDING),
                 ("full_name", ASCENDING)],
                name="crm_contact_profile_account_name",
            ),
            # Recently-touched order, for the saved views that ask for it.
            IndexModel(
                [("created_by", ASCENDING), ("is_archived", ASCENDING),
                 ("updated_at", DESCENDING)],
                name="crm_contact_profile_account_recent",
            ),
            IndexModel(
                [("created_by", ASCENDING), ("owner_id", ASCENDING)],
                name="crm_contact_profile_owner",
            ),
            # The company detail page's Contacts tab.
            IndexModel(
                [("created_by", ASCENDING), ("primary_company_id", ASCENDING)],
                name="crm_contact_profile_company",
            ),
            # Reverse lookup: "is this marketing contact worked by anyone
            # here?" — the join the contact list runs per page.
            IndexModel(
                [("contact_id", ASCENDING)],
                name="crm_contact_profile_contact",
            ),
        ]
