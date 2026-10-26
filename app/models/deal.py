"""DealModel — an opportunity moving through a pipeline. CRM-008.

The single most performance-sensitive model in the module. The compound index
``(created_by, pipeline_id, stage_id, position)`` is what makes the Kanban
board an IXSCAN instead of a COLLSCAN; the board endpoint's p95 budget
(< 500 ms for 1,000 deals across 10 stages, NFR1) depends on it.

Two fields are denormalised on purpose:

* ``status`` — derived from the stage's ``type``. Reporting, filtering and
  automation all need won/lost without joining the pipeline document. Written
  in the same operation as any stage change, in the service layer.
* ``primary_company_name`` — the Kanban renders a company name on every card.
  Without it, 7 columns x 25 cards costs 175 lookups. Refreshed when the
  association changes and when the company is renamed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import Field

from app.object_id import PyObjectId
from app.enums import DealStatus

from ._base import utcnow


class DealModel(Document):
    """A deal.

    ⚠️ **THERE IS NO `is_archived`, AND THAT IS A DECISION, NOT AN OVERSIGHT.**
    CRM-P0-006, decided 22 Oct 2026. Companies and contacts both have the
    field; deals deliberately do not.

    A deal you no longer want is marked **LOST**, not archived. That keeps it in
    the denominator for win rate, stage conversion and lost-reason reporting,
    which is what anybody asking "why did we lose these?" needs. Archiving it
    would remove it from the board and silently from those numbers — a user
    tidying their pipeline would be corrupting their own conversion rate without
    being told. `DealDetails`' delete dialog already steers towards marking it
    lost for exactly this reason.

    The consequence, accepted: deal deletion is PERMANENT, and bulk `archive` is
    absent from `bulk_rules.DEAL_ACTIONS`. `bulk_service` derives archive
    capability from `model_fields`, so the action is simply not offered for
    deals rather than failing per record — which is how this was found (bulk
    delete reported "0 updated, 2 failed" because `_delete(hard=False)` assigned
    a field that does not exist).

    **If this is ever revisited**, adding the field is not the work.
    `queries.deal_filter`, the board column totals and `forecast.py` must all
    learn to hide an archived deal FIRST — otherwise an archived deal keeps
    inflating the board totals and the weighted forecast, which is worse than
    not having the feature at all. Scope it as its own ticket.
    """

    # ── tenancy ──────────────────────────────────────────────────────────────
    created_by: PyObjectId
    workspace_id: Optional[PyObjectId] = None   # reserved, unused — see §4.2

    # ── identity & placement ─────────────────────────────────────────────────
    name: str
    pipeline_id: PyObjectId
    stage_id: PyObjectId                        # an embedded PipelineStage.id
    status: DealStatus = DealStatus.OPEN        # denormalised from stage.type
    # Fractional ordering within a column. Dropping between neighbours A and B
    # sets (A.position + B.position) / 2, so a drag is an O(1) single-document
    # write instead of renumbering the whole column. Renormalised when the gap
    # between neighbours falls below 1e-6. See position.py (CRM-015).
    position: float = 0.0

    # ── commercials ──────────────────────────────────────────────────────────
    amount: Optional[float] = None
    currency: Optional[str] = None
    expected_close_date: Optional[datetime] = None
    actual_close_date: Optional[datetime] = None
    probability: Optional[int] = None           # per-deal override of stage default

    # ── ownership & classification ───────────────────────────────────────────
    owner_id: Optional[PyObjectId] = None
    description: Optional[str] = None
    source: Optional[str] = None                # CrmRecordSource value
    tag_ids: List[PyObjectId] = Field(default_factory=list)
    custom_fields: Dict[str, Any] = Field(default_factory=dict)

    # ── close reasons ────────────────────────────────────────────────────────
    lost_reason_id: Optional[PyObjectId] = None   # from crm_settings.lost_reasons
    close_note: Optional[str] = None              # serves won and lost alike

    # ── denormalised for board rendering (avoids N+1 on the Kanban) ──────────
    primary_company_id: Optional[PyObjectId] = None
    primary_company_name: Optional[str] = None
    contact_count: int = 0
    open_task_count: int = 0
    overdue_task_count: int = 0
    next_activity_at: Optional[datetime] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    # Reset on every stage change. Drives both "days in stage" and the rotting
    # calculation, and is the input to days_in_previous_stage on the activity.
    stage_entered_at: datetime = Field(default_factory=utcnow)
    last_activity_at: Optional[datetime] = None
    activity_count: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by_user_id: Optional[PyObjectId] = None

    class Settings:
        name = "deals"
        indexes = [
            # ── THE board query. Do not reorder these keys. ──────────────────
            [("created_by", 1), ("pipeline_id", 1), ("stage_id", 1), ("position", 1)],
            [("created_by", 1), ("status", 1), ("updated_at", -1)],
            [("created_by", 1), ("owner_id", 1), ("status", 1)],
            [("created_by", 1), ("expected_close_date", 1)],
            [("created_by", 1), ("primary_company_id", 1)],
            [("created_by", 1), ("created_at", -1)],
        ]
