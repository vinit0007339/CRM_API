"""CrmContactModel — a contact as a CRM object. E25 / CRM-P2-000.

⚠️ **WHY THIS IS NOT THE PLATFORM `contacts` COLLECTION.** `Contact` in
`app/db/models/main/contacts.py` declares::

    email: Optional[str] = Indexed(unique=True)

GLOBALLY unique, across every account, with `created_by` plus an `owner_ids`
list so one document can be shared between accounts. That is a deliberate
marketing-side design — one person, one email, many lists — and it is hostile
to a per-account CRM in two ways that no amount of extra fields fixes:

1. Two accounts cannot both hold `priya@acme.com`. The second one's import
   dies on a duplicate key, and there is nothing sensible to tell that user.
2. Per-account CRM fields have no owner on a shared document. Whose
   `owner_id`? Which account's `lifecycle_stage`? Which account's
   `custom_fields`, when each defines its own attributes?

So a CRM contact is its own document, scoped the way every other CRM object is
— `created_by == account_id` — with `(created_by, email)` unique: per-account,
not global. Campaigns, lists and segments keep their collection untouched.

⚠️ **THERE IS NO LINK TO THE PLATFORM CONTACT, AND THAT IS DELIBERATE.** This
model carried a `contact_id` pointing into `contacts`, set when somebody added
a CRM contact to a marketing list. That seam is gone: the CRM reads and writes
`crm_contacts` and nothing else.

The reason is that one seam produced two populations of the same person with no
rule about which was authoritative — and the ambiguity reached the association
collection, where a `contact` row could hold either a platform id or a CRM one
depending on which screen wrote it. That is what CRM-406 was parked on, and
severing the link is the answer to it.

The consequence, stated plainly because somebody will ask: **a CRM contact is
not marketable.** They cannot be added to a list or emailed from the CRM. To
market to them, they are imported into the marketing audience as a contact in
their own right, where they get their own consent record — which is the only
honest way for a sales record to become a subscriber anyway.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class CrmContactModel(Document):
    created_by: PyObjectId                      # account tenancy
    created_by_user_id: Optional[PyObjectId] = None
    owner_id: Optional[PyObjectId] = None       # the CRM owner, per account

    # ── identity ────────────────────────────────────────────────────────────
    # Stored lowercased and trimmed by `contact_rules.normalize_email`. The
    # unique index below is on this field, so the normalisation IS the dedupe
    # key — writing a raw address here would let "Priya@Acme.com" and
    # "priya@acme.com" both exist in one account.
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    # Denormalised for sorting and search: a list sorting on "name" cannot sort
    # on two columns, and an index on a computed value does not exist.
    full_name: Optional[str] = None
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    job_title: Optional[str] = None
    linkedin_url: Optional[str] = None

    # ── CRM shape ───────────────────────────────────────────────────────────
    lifecycle_stage: Optional[str] = None       # see contact_rules.LIFECYCLE
    primary_company_id: Optional[PyObjectId] = None
    # Denormalised company name, same trade-off as `DealModel`: the list shows
    # it on every row, and `CompanyCrud.rename_fanout` keeps it current.
    primary_company_name: Optional[str] = None
    tag_ids: List[PyObjectId] = Field(default_factory=list)
    custom_fields: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    source: Optional[str] = None                # manual | import | auto_link

    # ── rollups and lifecycle ───────────────────────────────────────────────
    deal_count: int = 0
    open_deal_value: float = 0.0
    open_task_count: int = 0
    overdue_task_count: int = 0
    last_activity_at: Optional[datetime] = None
    is_archived: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_contacts"
        indexes = [
            # ⚠️ PER-ACCOUNT UNIQUE, which is the whole point of this
            # collection: `(created_by, email)`, not the platform's global
            # `email`.
            #
            # ⚠️ PARTIAL, **NOT** `sparse=True` — AND THE ORIGINAL HAD THIS
            # WRONG. The intent was stated correctly in the comment it carried
            # ("a contact may legitimately have no email — a phone-only lead")
            # but `sparse` does not deliver it on a COMPOUND index. MongoDB's
            # rule is that a compound sparse index "only indexes documents that
            # contain a value for at least one of the keys" — and `created_by`
            # is always present, so EVERY document is indexed, with a missing
            # `email` stored as null. Two phone-only contacts in one account
            # therefore both key on `(account, null)` and the second insert
            # fails with a duplicate key error.
            #
            # `sparse` would have worked on a single-field index on `email`
            # alone, which is what the MongoDB example demonstrates and is
            # probably where it came from. For the compound case the predicate
            # has to be explicit.
            #
            # ⚠️ IF `crm_contacts` STILL EXISTS IN A DATABASE, the old
            # `uniq_crm_contact_email` is still on it — the removal migration
            # deliberately dropped nothing. Creating this one under the same
            # name with different options fails with IndexOptionsConflict, so
            # that index must be DROPPED before this model is registered. See
            # docs/lead-phases.md.
            IndexModel(
                [("created_by", ASCENDING), ("email", ASCENDING)],
                unique=True,
                partialFilterExpression={"email": {"$type": "string"}},
                name="uniq_crm_contact_email",
            ),
            # The list's default sort and its owner filter.
            IndexModel(
                [("created_by", ASCENDING), ("is_archived", ASCENDING),
                 ("full_name", ASCENDING)],
                name="crm_contact_account_name",
            ),
            IndexModel(
                [("created_by", ASCENDING), ("owner_id", ASCENDING)],
                name="crm_contact_owner",
            ),
            # The company detail page's Contacts tab.
            IndexModel(
                [("created_by", ASCENDING), ("primary_company_id", ASCENDING)],
                name="crm_contact_company",
            ),
        ]
