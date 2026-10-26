"""PipelineModel with embedded stages. CRM-007.

Stages are EMBEDDED, not a separate collection. A pipeline has 3-20 stages and
they are always loaded together with it, so embedding means:

* the Kanban board endpoint reads one document to know its columns,
* reordering is a single write,
* there is no orphan-stage class of bug.

The cost — renaming a stage rewrites the parent document — is negligible at
this cardinality. Deals reference ``stage_id``, the embedded stage's own
ObjectId, which is stable across renames and reorders.

Invariants (3-20 stages, exactly one won + one lost pinned last, unique names,
contiguous order, one default pipeline per account) are enforced in
``api/services/engage/crm/stage_rules.py`` — pure logic, unit-tested, CRM-013.
This model deliberately does not enforce them: Beanie validation can't express
cross-document rules like "exactly one default per account".
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from beanie import Document
from pydantic import BaseModel, Field

from app.object_id import PyObjectId
from app.enums import StageType

from ._base import utcnow


class PipelineStage(BaseModel):
    """A Kanban column. Embedded in PipelineModel.stages."""

    # Generated once at creation and never regenerated — deals point at it.
    id: PyObjectId = Field(default_factory=PyObjectId)
    name: str
    order: int = 0                       # 0-based, contiguous after any mutation
    type: StageType = StageType.OPEN
    # Used for weighted forecasting. Forced to 100 on the won stage and 0 on
    # the lost stage by stage_rules.py.
    win_probability: int = 0
    color: Optional[str] = None
    # internal_names that must be set before a deal may ENTER this stage.
    # Enforced on the stage-change endpoint (CRM-037), which returns the
    # missing field labels so the UI can prompt for exactly those.
    required_attributes: List[str] = Field(default_factory=list)
    # A deal sitting here longer than this many days is flagged "rotting".
    # Computed server-side; the UI must not recompute it.
    rotting_days: Optional[int] = None


class PipelineModel(Document):
    created_by: PyObjectId
    workspace_id: Optional[PyObjectId] = None   # reserved, unused — see §4.2

    name: str
    description: Optional[str] = None
    is_default: bool = False
    is_archived: bool = False
    stages: List[PipelineStage] = Field(default_factory=list)
    currency: Optional[str] = None
    weighted_forecast_enabled: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "pipelines"
        indexes = [
            [("created_by", 1), ("is_archived", 1), ("created_at", 1)],
            [("created_by", 1), ("is_default", 1)],
        ]
