"""Company request/response schemas — CRM-004 frozen contract.

Routes: ``/api/v1/crm/companies``. Tenancy is ``created_by == account_id``;
it is never accepted from the client and never returned as a settable field.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId

from .common import (
    CompanyRef,
    DuplicateWarning,
    LinkedCollection,
    TagRef,
    UserRef,
)


class CompanyAddressSchema(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class CompanyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    domain: Optional[str] = Field(
        None,
        description=(
            "Normalised server-side: scheme, www., port, path and trailing "
            "slash stripped, lowercased. 'HTTPS://WWW.Acme.com/about' -> 'acme.com'"
        ),
    )
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = Field(
        None, description="Value from crm_settings.industries — a picklist, not free text"
    )
    employee_count: Optional[int] = Field(None, ge=0)
    annual_revenue: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, description="ISO-4217; defaults from crm_settings")
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    address: Optional[CompanyAddressSchema] = None
    description: Optional[str] = Field(None, max_length=5000)
    owner_id: Optional[PyObjectId] = None
    tag_ids: List[PyObjectId] = []
    custom_fields: Dict[str, Any] = Field(
        {}, description="Keyed by CrmAttribute.internal_name"
    )
    # Convenience only — creates associations at create time. On UPDATE these
    # are NOT accepted; use POST/DELETE /{id}/links instead.
    contact_ids: List[PyObjectId] = []
    deal_ids: List[PyObjectId] = []


class CompanyUpdateRequest(BaseModel):
    """Every field optional.

    Deliberately has no ``contact_ids`` / ``deal_ids``: a PATCH must never
    replace an association set. Brevo's PATCH wipes any linked id not present
    in the payload, which is why they needed a second endpoint to work around
    it — we simply don't offer the footgun. See CRM plan §3.3 / CRM-029.
    """

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    domain: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = Field(None, ge=0)
    annual_revenue: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    address: Optional[CompanyAddressSchema] = None
    description: Optional[str] = Field(None, max_length=5000)
    owner_id: Optional[PyObjectId] = None
    tag_ids: Optional[List[PyObjectId]] = None
    custom_fields: Optional[Dict[str, Any]] = None
    is_archived: Optional[bool] = None

    # Unknown fields are rejected, not dropped. Silently ignoring a
    # `stage_id` on a PATCH would let a client believe it moved a deal.
    model_config = {"extra": "forbid"}


class CompanyDetails(BaseModel):
    # Reads `_id` from Mongo, writes `id` on the wire. FastAPI serialises
    # response_model with by_alias=True, so without serialization_alias every
    # client would receive `_id` while every screen reads `.id`.
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    name: str
    domain: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    annual_revenue: Optional[float] = None
    currency: Optional[str] = None
    phone: Optional[str] = None
    phone_country_code: Optional[str] = None
    address: Optional[CompanyAddressSchema] = None
    description: Optional[str] = None

    owner: Optional[UserRef] = None
    tags: List[TagRef] = []
    custom_fields: Dict[str, Any] = {}

    # Denormalised rollups — display only, never read by business logic, so
    # drift can't cause incorrect behaviour. Reconciled by a nightly job.
    contact_count: int = 0
    deal_count: int = 0
    open_deal_value: float = 0.0
    won_deal_value: float = 0.0

    source: Optional[str] = None
    is_archived: bool = False
    last_activity_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    # Present on the single-record endpoint only (first 20 each + true total),
    # so one request paints the whole detail page.
    linked_contacts: Optional[LinkedCollection] = None
    #: CRM-owned people, from `crm_contacts`. A separate collection in the
    #: payload because they are a separate population: a CRM contact is NOT
    #: marketable, and merging the two lists would invite a client to email
    #: someone who never consented. Omitted (None) on the list endpoint, like
    #: its siblings.
    linked_crm_contacts: Optional[LinkedCollection] = None
    linked_deals: Optional[LinkedCollection] = None

    # Set on CREATE when a probable duplicate exists. Non-blocking.
    duplicate_warning: Optional[DuplicateWarning] = None

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class CompanyListResponse(BaseModel):
    data: List[CompanyDetails] = []
    total: int = 0
    status: str = "success"
    # ⚠️ WITHOUT THIS A LIST FAILURE IS SILENT. The handler already answers
    # `status: "failure"` with a message for a filter it cannot read; pydantic
    # DROPS an undeclared field, so the UI got an empty list and no reason for
    # it. Every other CRM list envelope declares this one.
    message: Optional[str] = ""


class CompanyLinksRequest(BaseModel):
    #: ⚠️ A FIELD OF ITS OWN, NOT MIXED INTO `contact_ids`. The two carry ids
    #: from different collections — `contacts` and `crm_contacts` — and a
    #: single list would make the row's meaning depend on which screen posted
    #: it, which is the ambiguity `AssociationEntity.CRM_CONTACT` exists to
    #: remove. Both default to empty, so every existing client is unaffected.
    crm_contact_ids: List[PyObjectId] = []
    """POST = additive, DELETE = subtractive. Never a replace."""

    contact_ids: List[PyObjectId] = []
    deal_ids: List[PyObjectId] = []


class CompanyMergeRequest(BaseModel):
    survivor_id: PyObjectId
    merged_id: PyObjectId
    field_resolution: Dict[str, str] = Field(
        {}, description='field -> "survivor" | "merged"; survivor wins by default'
    )

    @model_validator(mode="after")
    def not_the_same_record(self):
        # Merging a company into itself is NOT a no-op: the loser is archived
        # at the end, so it reads as a harmless mistake and is a deletion.
        # Also checked in merge_rules.validate, which protects the paths that
        # do not come through this schema.
        if str(self.survivor_id) == str(self.merged_id):
            raise ValueError("A company can't be merged into itself.")
        return self


class CompanyDuplicatePair(BaseModel):
    left: CompanyRef
    right: CompanyRef
    confidence: str
    matched_on: str
