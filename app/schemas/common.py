"""Shared CRM schema primitives — envelopes, enums and query params.

CRM-004: this module is part of the frozen API contract. The frontend is built
against these shapes before the route implementations land, so changing a field
name here is a breaking change that needs a note in the PR.

Two response envelopes exist across Engage and the CRM module follows both,
matching the rest of the codebase rather than inventing a third:

* single object  ->  ``{"status": ..., "message": ..., "data": {...}}``
* list           ->  ``{"data": [...], "total": N, "status": ...}``

And two *error* shapes coexist, which is inherited, not chosen:

* raised inside a route body  ->  HTTP 200 with ``status="failure"``
  (this includes ``PermissionDeniedError`` — deliberately a 200 so the UI can
  render the message inline instead of tripping the global 401/403 handler)
* pydantic validation failure ->  HTTP 422 ``{"messages": [...], "success": false}``
  from the global handler in ``api/main.py``

The client service layer (``src/Utility/crm.js``) normalises both into one shape
so screens never branch on it.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

from app.object_id import PyObjectId

T = TypeVar("T")


# ── Envelopes ────────────────────────────────────────────────────────────────
class ResponseSchema(BaseModel, Generic[T]):
    """Single-object envelope."""

    status: str = Field(..., description='"success" or "failure"')
    message: Optional[str] = ""
    data: Optional[T] = None


class ListResponseSchema(BaseModel, Generic[T]):
    """List envelope. Note the flat shape — `data`/`total`/`status` are siblings.

    ``total`` is the count of the *filtered* set, not the page.
    """

    data: List[T] = []
    total: int = 0
    status: str = "success"


# ── Enums ────────────────────────────────────────────────────────────────────
# Imported, not redefined. app/enums/crm.py is the single source of truth for
# both the Beanie models and these schemas — two copies of StageType is exactly
# how the API starts accepting a value the model rejects.
from app.enums import (  # noqa: E402  (kept here so the enum block reads together)
    ActivitySource,
    ActivityType,
    AssociationEntity,
    AttributeType,
    CrmObjectType,
    CrmRecordSource,
    DealStatus,
    ImportStatus,
    StageType,
)


class SortDirection(str, Enum):
    """API-only: never persisted, so it has no model-layer counterpart."""

    ASC = "asc"
    DESC = "desc"


# ── Shared fragments ─────────────────────────────────────────────────────────
class UserRef(BaseModel):
    """Expanded owner/actor. Batched in a single lookup per page — never N+1."""

    id: PyObjectId
    name: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None


class TagRef(BaseModel):
    id: PyObjectId
    name: str
    color: Optional[str] = None


class CompanyRef(BaseModel):
    id: PyObjectId
    name: str
    domain: Optional[str] = None


class ContactRef(BaseModel):
    id: PyObjectId
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[str] = Field(
        None, description='Association role, e.g. "decision_maker", "champion"'
    )


class LinkedCollection(BaseModel):
    """A truncated set of linked records plus the true total."""

    data: List[Dict[str, Any]] = []
    total: int = 0


class DuplicateWarning(BaseModel):
    """Returned on company create when the domain or name collides.

    A *warning*, not a rejection — Brevo hard-blocks duplicate domains, which
    breaks subsidiaries, franchises and regional entities sharing a parent
    domain. See CRM plan §3.3.
    """

    id: PyObjectId
    name: str
    confidence: str = Field(..., description='"high" | "medium" | "low"')
    matched_on: str = Field(..., description='"domain" | "name"')


class ActivityDetails(BaseModel):
    # Reads `_id` from Mongo, writes `id` on the wire. FastAPI serialises
    # response_model with by_alias=True, so without serialization_alias every
    # client would receive `_id` while every screen reads `.id`.
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    entity_type: AssociationEntity
    entity_id: PyObjectId
    activity_type: str
    actor: Optional[UserRef] = None
    source: str = "ui"
    title: str = Field(..., description="Pre-rendered server-side; the UI does no string assembly")
    body: Optional[str] = None
    metadata: Dict[str, Any] = {}
    occurred_at: datetime

    model_config = {"populate_by_name": True}


class BulkActionRequest(BaseModel):
    """Bulk operations accept *either* explicit ids or the current filter set.

    Passing ``filters`` is what powers "select all N matching this filter" —
    the client must never have to enumerate ids for that case.
    """

    ids: Optional[List[PyObjectId]] = None
    filters: Optional[Dict[str, Any]] = None
    # The advanced filter (E22), as the GROUP the user built — never as a Mongo
    # fragment. The route compiles it with `filter_rules`, which is the only
    # thing that decides what query a client may cause. A group sent here is
    # the same shape the list route takes in `?filters=`, so "select all
    # matching" acts on exactly the set that was on screen.
    filter_group: Optional[Dict[str, Any]] = None
    action: str
    payload: Dict[str, Any] = {}


class BulkActionResult(BaseModel):
    matched: int = Field(
        0, description="How many records the selection matched, BEFORE the cap. "
                       "Reported so a capped run is distinguishable from a "
                       "complete one — a silent truncation looks like success.",
    )
    updated: int = 0
    skipped: int = Field(
        0, description="Already as requested, or excluded by a business rule",
    )
    failed: int = 0
    # ADDITIVE to the Phase 1 contract: existing clients keep working, and
    # "12 of 40 updated" stops being unexplainable. Permission, a business rule
    # and an error are three different problems with three different fixes, so
    # they are three different counts.
    skipped_no_permission: int = 0
    skipped_reason: Optional[str] = Field(
        None, description='e.g. "closed deals were left where they are"'
    )
