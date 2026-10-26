"""CrmActivityModel — the append-only CRM timeline. CRM-010.

Modelled on the shipped ``AuditLogModel``: generic ``entity_type`` /
``entity_id``, arbitrary ``metadata``, never updated, never edited.

DO NOT build on the ``events/`` package instead. It is broken:
``events/service.py::insert_event`` returns None from a shadowed nested
definition, ``events/router.py`` imports a non-existent ``events.schema``, and
``lookup_user_profile_id`` queries a ``contact`` collection that does not exist.

This model must ship in Phase 1 even though reporting is Phase 4: stage-duration
analysis can only read history that was recorded from day one. Deals created
before the recorder exists have no history to report on, permanently.

All writes go through ``api/services/engage/crm/activity_recorder.py`` (CRM-014),
never directly from a route.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from beanie import Document
from pydantic import Field

from app.object_id import PyObjectId
from app.enums import ActivitySource, AssociationEntity

from ._base import utcnow


class CrmActivityModel(Document):
    created_by: PyObjectId

    entity_type: AssociationEntity
    entity_id: PyObjectId
    # ActivityType value. Kept as a plain str so a new activity kind can be
    # written by a hotfix without a model migration; the enum is the catalogue,
    # not a hard constraint.
    activity_type: str

    actor_user_id: Optional[PyObjectId] = None
    actor_name: Optional[str] = None          # snapshot; survives user deletion
    source: ActivitySource = ActivitySource.UI

    # Pre-rendered server-side so the timeline UI does no string assembly and
    # historical entries don't change meaning when labels are edited later.
    title: str
    body: Optional[str] = None
    # e.g. {"from_stage": "...", "to_stage": "...", "days_in_previous_stage": 9}
    metadata: Dict[str, Any] = Field(default_factory=dict)

    occurred_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_activities"
        indexes = [
            # The timeline query.
            [("created_by", 1), ("entity_type", 1), ("entity_id", 1), ("occurred_at", -1)],
            [("created_by", 1), ("occurred_at", -1)],
            [("created_by", 1), ("activity_type", 1), ("occurred_at", -1)],
        ]
