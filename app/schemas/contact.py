"""CRM contact request/response schemas. E25.

Routes: ``/api/v1/crm/contacts``. Tenancy is ``created_by == account_id``; it
is never accepted from the client and never returned as a settable field.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.object_id import PyObjectId

from .common import CompanyRef, UserRef


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
