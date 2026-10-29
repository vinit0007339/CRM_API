"""Deal + Kanban board schemas — CRM-004 frozen contract.

Routes: ``/api/v1/crm/deals`` and ``/api/v1/crm/pipelines/{id}/board``.

The board response is the highest-traffic shape in the module and the frontend
is built directly against it — one request paints the whole board. Changing a
field here breaks KanbanBoard.jsx.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId

from .common import CompanyRef, ContactRef, DealStatus, StageType, TagRef, UserRef


class DealCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    pipeline_id: Optional[PyObjectId] = Field(
        None, description="Defaults to the account's default pipeline"
    )
    stage_id: Optional[PyObjectId] = Field(
        None, description="Defaults to the first open stage of the pipeline"
    )
    amount: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None
    expected_close_date: Optional[datetime] = None
    probability: Optional[int] = Field(
        None, ge=0, le=100, description="Per-deal override of the stage default"
    )
    owner_id: Optional[PyObjectId] = None
    description: Optional[str] = Field(None, max_length=5000)
    source: Optional[str] = None
    tag_ids: List[PyObjectId] = []
    custom_fields: Dict[str, Any] = {}
    company_ids: List[PyObjectId] = []
    primary_company_id: Optional[PyObjectId] = None
    contact_ids: List[PyObjectId] = []
    #: CRM-owned people. See `DealLinksRequest.crm_contact_ids`.
    crm_contact_ids: List[PyObjectId] = []


class DealUpdateRequest(BaseModel):
    """Every field optional. No ``stage_id`` and no association fields — by design.

    Stage moves go through ``PATCH /crm/deals/{id}/stage`` exclusively so that
    stage-rule validation, fractional positioning and activity recording can
    never be bypassed. A PATCH carrying ``stage_id`` is rejected with a message
    pointing at the stage endpoint (CRM-034).
    """

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    amount: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = None
    expected_close_date: Optional[datetime] = None
    probability: Optional[int] = Field(None, ge=0, le=100)
    owner_id: Optional[PyObjectId] = None
    description: Optional[str] = Field(None, max_length=5000)
    source: Optional[str] = None
    tag_ids: Optional[List[PyObjectId]] = None
    custom_fields: Optional[Dict[str, Any]] = None

    # Unknown fields are rejected, not dropped. Silently ignoring a
    # `stage_id` on a PATCH would let a client believe it moved a deal.
    model_config = {"extra": "forbid"}


class DealStageChangeRequest(BaseModel):
    """The drag-and-drop payload (CRM-037).

    ``before_deal_id`` / ``after_deal_id`` are the cards the dropped card landed
    between; the server derives the fractional ``position`` from them. Omit both
    to append to the end of the column.
    """

    stage_id: PyObjectId
    pipeline_id: Optional[PyObjectId] = Field(
        None, description="Only when moving across pipelines"
    )
    before_deal_id: Optional[PyObjectId] = None
    after_deal_id: Optional[PyObjectId] = None


class DealCloseRequest(BaseModel):
    outcome: DealStatus = Field(..., description="won | lost")
    amount: Optional[float] = Field(None, ge=0)
    actual_close_date: Optional[datetime] = None
    lost_reason_id: Optional[PyObjectId] = Field(
        None, description="Required when outcome=lost; from crm_settings.lost_reasons"
    )
    close_note: Optional[str] = Field(None, max_length=2000)


class DealReopenRequest(BaseModel):
    stage_id: PyObjectId = Field(..., description="Must be an OPEN stage")


class DealLinksRequest(BaseModel):
    company_ids: List[PyObjectId] = []
    #: PLATFORM contacts — the marketing audience, `to_type="contact"`.
    contact_ids: List[PyObjectId] = []
    #: CRM-OWNED people, `to_type="crm_contact"`.
    #:
    #: ⚠️ WITHOUT THIS A CRM CONTACT COULD NOT BE PUT ON A DEAL AT ALL. The
    #: company page's "+ New contact" creates a `crm_contacts` row and the
    #: company can hold it (`CompanyLinksRequest.crm_contact_ids`), but a deal
    #: had only the platform field — so a person created in the CRM could be
    #: attached to their company and never to the work. Two separate lists
    #: rather than one guessing field, for the reason `AssociationEntity` gained
    #: a second member: the row names the collection its id belongs to, so
    #: neither is ever searched for the other's id.
    crm_contact_ids: List[PyObjectId] = []
    primary_company_id: Optional[PyObjectId] = None


class StageChangeRejection(BaseModel):
    """The ``data`` of a refused stage move.

    WHY THIS EXISTS. ``PATCH /deals/{id}/stage`` is declared
    ``response_model=ResponseSchema[DealDetails]``, and FastAPI validates the
    OUTGOING response against it. The rejection path returns
    ``data={"missing_attributes": [...]}``, which is not a DealDetails — so
    FastAPI raised ResponseValidationError and the user got a **500** instead of
    the field list. It only happened when the target stage had unmet required
    attributes, which is why it looked intermittent.

    ``missing_attributes`` is REQUIRED, deliberately: it makes the union with
    DealDetails unambiguous in either direction. A DealDetails payload has no
    ``missing_attributes`` and a rejection has no ``name``/``created_at``, so
    neither can validate as the other.
    """

    missing_attributes: List[Dict[str, str]] = Field(
        ...,
        description="{internal_name, label} pairs. The client renders an input "
                    "per field and sends the value back under internal_name — a "
                    "bare label cannot be matched back to a field once an "
                    "account renames the attribute.",
    )


class DealDetails(BaseModel):
    # Reads `_id` from Mongo, writes `id` on the wire. FastAPI serialises
    # response_model with by_alias=True, so without serialization_alias every
    # client would receive `_id` while every screen reads `.id`.
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    name: str
    pipeline_id: PyObjectId
    stage_id: PyObjectId
    stage_name: Optional[str] = None
    status: DealStatus = DealStatus.OPEN
    position: float = 0.0

    amount: Optional[float] = None
    currency: Optional[str] = None
    expected_close_date: Optional[datetime] = None
    actual_close_date: Optional[datetime] = None
    probability: Optional[int] = None
    weighted_amount: Optional[float] = None

    owner: Optional[UserRef] = None
    description: Optional[str] = None
    source: Optional[str] = None
    tags: List[TagRef] = []
    custom_fields: Dict[str, Any] = {}

    lost_reason_id: Optional[PyObjectId] = None
    lost_reason_label: Optional[str] = None
    close_note: Optional[str] = None

    primary_company: Optional[CompanyRef] = None
    contacts: List[ContactRef] = []
    contact_count: int = 0

    open_task_count: int = 0
    overdue_task_count: int = 0
    next_activity_at: Optional[datetime] = None
    is_rotting: bool = Field(
        False,
        description=(
            "Computed server-side from stage_entered_at + stage.rotting_days. "
            "The UI must not recompute it."
        ),
    )

    stage_entered_at: Optional[datetime] = None
    days_in_stage: Optional[int] = None
    last_activity_at: Optional[datetime] = None
    activity_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class DealListSummary(BaseModel):
    """Totals over the WHOLE filtered set, not the page. E24.

    ⚠️ `unweighted_count` IS NOT DECORATION. A deal whose pipeline has been
    deleted has no stage to weight it by, so it counts toward `total_value`
    and is left out of `weighted_value`. Rendering the weighted figure without
    saying some deals are missing from it is the quiet kind of wrong.
    """

    deal_count: int = 0
    total_value: float = 0.0
    weighted_value: float = 0.0
    # "Pipeline value" means OPEN deals everywhere in this UI.
    open_total_value: float = 0.0
    open_weighted_value: float = 0.0
    unweighted_count: int = 0


class DealListResponse(BaseModel):
    data: List[DealDetails] = []
    total: int = 0
    status: str = "success"
    # Additive: the list envelope needs somewhere to put a business failure
    # message (e.g. a malformed pipeline_id). Absent on success.
    message: Optional[str] = ""
    # E24. Computed by an aggregation over the same filter as `data`, so the
    # footer cannot add up a different set from the rows above it. `None` when
    # the sums could not be computed — a missing footer, not a wrong one.
    summary: Optional[DealListSummary] = None


# ── Kanban board ─────────────────────────────────────────────────────────────
class BoardCard(BaseModel):
    """The trimmed deal shape rendered on a Kanban card.

    Deliberately smaller than DealDetails — a board can hold hundreds of these
    and the card only renders a handful of fields.
    """

    # Reads `_id` from Mongo, writes `id` on the wire. FastAPI serialises
    # response_model with by_alias=True, so without serialization_alias every
    # client would receive `_id` while every screen reads `.id`.
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    name: str
    amount: Optional[float] = None
    currency: Optional[str] = None
    position: float = 0.0
    expected_close_date: Optional[datetime] = None
    # The card's own probability override, and what it contributes to its
    # column's weighted total. Present so the board's promise — a column header
    # never disagrees with the cards beneath it — is something a client can
    # actually verify, instead of a claim only the server can check.
    probability: Optional[int] = None
    weighted_amount: Optional[float] = None
    owner: Optional[UserRef] = None
    primary_company: Optional[CompanyRef] = None
    contact_count: int = 0
    tags: List[TagRef] = []
    open_task_count: int = 0
    overdue_task_count: int = 0
    next_activity_at: Optional[datetime] = None
    is_rotting: bool = False
    custom_fields: Dict[str, Any] = {}

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class BoardStage(BaseModel):
    """One Kanban column.

    Counts and sums honour the board's active filters, so a column header can
    never disagree with the cards visible beneath it.
    """

    id: PyObjectId
    name: str
    order: int
    type: StageType = StageType.OPEN
    win_probability: int = 0
    color: Optional[str] = None
    required_attributes: List[str] = []
    deal_count: int = 0
    total_value: float = 0.0
    weighted_value: float = 0.0
    has_more: bool = False
    deals: List[BoardCard] = []


class BoardPipelineMeta(BaseModel):
    id: PyObjectId
    name: str
    currency: Optional[str] = None
    weighted_forecast_enabled: bool = False


class BoardTotals(BaseModel):
    deal_count: int = 0
    total_value: float = 0.0
    weighted_value: float = 0.0


class BoardData(BaseModel):
    pipeline: BoardPipelineMeta
    totals: BoardTotals
    stages: List[BoardStage] = []


class BoardResponse(BaseModel):
    status: str = "success"
    message: Optional[str] = ""
    data: Optional[BoardData] = None
