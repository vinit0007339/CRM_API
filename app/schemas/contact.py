"""CRM contact request/response schemas. E25.

Routes: ``/api/v1/crm/contacts``. Tenancy is ``created_by == account_id``; it
is never accepted from the client and never returned as a settable field.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.object_id import PyObjectId

from .common import CompanyRef, LinkedCollection, UserRef


class ContactPromoteRequest(BaseModel):
    """Take up a platform contact as a CRM-owned person."""

    platform_contact_id: PyObjectId
    owner_id: Optional[PyObjectId] = None
    #: Defaults to `lead` — see the note in the route. The platform has no
    #: lifecycle field, so there is nothing to copy and this is a decision.
    lifecycle_stage: Optional[str] = None


class ContactCreateRequest(BaseModel):
    """⚠️ EVERY FIELD IS OPTIONAL, INCLUDING THE EMAIL.

    A phone-only lead from a trade show is a real contact, and the unique
    index is `sparse` for that reason. What IS required is some identity —
    a name or an address — which `contact_rules.has_identity` enforces,
    because a record with neither renders as "Unnamed contact" and can never
    be found again.
    """

    email: Optional[str] = Field(None, max_length=254)
    first_name: Optional[str] = Field(None, max_length=120)
    last_name: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    job_title: Optional[str] = Field(None, max_length=120)
    linkedin_url: Optional[str] = None
    lifecycle_stage: Optional[str] = Field(
        None, description="subscriber | lead | qualified | customer | churned")
    owner_id: Optional[PyObjectId] = None
    primary_company_id: Optional[PyObjectId] = None
    tag_ids: List[PyObjectId] = Field(default_factory=list)
    description: Optional[str] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict)


class ContactUpdateRequest(BaseModel):
    """PATCH: absent means unchanged.

    ⚠️ The route reads `model_dump(exclude_unset=True)`, not a None check.
    Clearing a contact's job title or company is a real edit, and a None check
    would read it as "unchanged" — so the button would silently do nothing.
    """

    email: Optional[str] = Field(None, max_length=254)
    first_name: Optional[str] = Field(None, max_length=120)
    last_name: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    job_title: Optional[str] = Field(None, max_length=120)
    linkedin_url: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    owner_id: Optional[PyObjectId] = None
    primary_company_id: Optional[PyObjectId] = None
    tag_ids: Optional[List[PyObjectId]] = None
    description: Optional[str] = None
    custom_fields: Optional[Dict[str, Any]] = None
    is_archived: Optional[bool] = None


class ContactDuplicate(BaseModel):
    """The contact already holding this address.

    ⚠️ A REJECTION, NOT A WARNING — unlike a company's duplicate domain. An
    email is UNIQUE per account (`uniq_crm_contact_email`), so "create anyway"
    is not an option the database will honour: it would fail on the index. The
    id is here so the UI can offer "Open existing" instead of a dead end.
    """

    id: PyObjectId
    full_name: Optional[str] = None
    email: Optional[str] = None


class ContactMarketing(BaseModel):
    """The person's standing with marketing, as the CRM may SHOW it.

    ⚠️ READ-ONLY, AND NOTHING IN THE CRM WRITES IT. Subscription is a consent
    record; it is changed on the marketing side, by the person or by somebody
    acting on their request. The CRM renders it so a rep can see that the
    contact they are about to email through a campaign has not opted in —
    which is the question this panel exists to answer.

    ⚠️ `None` ON THE LEGACY PATH, AND THAT IS NOT THE SAME AS "not
    subscribed". A legacy CRM contact has no marketing record at all; saying
    `subscribed: false` would assert a consent state for somebody the audience
    has never heard of.
    """

    subscribed: bool = False
    subscribe_date: Optional[datetime] = None
    #: Count, not ids: the CRM has no business rendering another module's
    #: list names, and the number is what tells a rep whether this person is
    #: being mailed at all.
    list_count: int = 0


class ContactDetails(BaseModel):
    id: Optional[PyObjectId] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: str = ""
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    job_title: Optional[str] = None
    linkedin_url: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    owner: Optional[UserRef] = None
    primary_company: Optional[CompanyRef] = None
    tag_ids: List[PyObjectId] = Field(default_factory=list)
    custom_fields: Dict[str, Any] = Field(default_factory=dict)

    # The platform link, as a state AND an id: the UI shows "in your marketing
    # audience" and needs the id to deep-link into the contact record there.

    deal_count: int = 0
    open_deal_value: float = 0.0
    open_task_count: int = 0
    overdue_task_count: int = 0

    #: The deals this person is on, and the companies they belong to.
    #:
    #: ⚠️ THE COUNTERPART TO `CompanyDetails.linked_deals`, AND THE REASON THE
    #: CONTACT PAGE WAS A DEAD END. `deal_count` and `open_deal_value` were
    #: already here, so the page could say "3 deals" and then offer no way to
    #: see or reach one — the association existed in `crm_associations`, was
    #: writable from the deal side (`DealLinksRequest.crm_contact_ids`), and
    #: was readable from nowhere. A rollup without its collection is a number
    #: the user cannot act on.
    #:
    #: Both are TRUNCATED to the first `LINKED_PREVIEW`, with `total` carrying
    #: the true count — same contract as the company page, so the "Showing N
    #: of M" line reads identically on both screens.
    #:
    #: `None` rather than an empty collection when the route did not populate
    #: them: the list endpoint returns `ContactDetails` too, and 50 rows must
    #: not each carry two association lookups. Absent means "not asked for";
    #: `{"data": [], "total": 0}` means "asked, and there are none".
    linked_deals: Optional[LinkedCollection] = None
    linked_companies: Optional[LinkedCollection] = None

    #: Present and populated only on the unified path — see ContactMarketing.
    marketing: Optional[ContactMarketing] = None

    is_archived: bool = False
    last_activity_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Present on CREATE when the address is already taken. See ContactDuplicate.
    duplicate: Optional[ContactDuplicate] = None


class ContactListResponse(BaseModel):
    data: List[ContactDetails] = []
    total: int = 0
    status: str = "success"
    # Every CRM list envelope declares this; without it a business failure
    # reaches the UI as an empty list with no reason for it.
    message: Optional[str] = ""
