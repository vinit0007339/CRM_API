"""CrmAssociationModel — one join collection for every CRM relationship. CRM-009.

Company<->Contact, Company<->Deal and Deal<->Contact are all many-to-many and
all live here, following the shipped ``list_memberships`` join-collection
pattern.

Why a join collection rather than id arrays on each parent, which is what Brevo
does: their arrays are two-way, and their ``PATCH`` replaces one wholesale —
silently unlinking anything absent from the payload. They needed a second
endpoint to work around it. One row per relationship, with a unique index,
removes the entire class of bug.

CANONICAL DIRECTION: a pair is stored in exactly one direction, ordered by the
precedence company < deal < contact (the member order of ``AssociationEntity``).
So a deal<->contact link is always ``from_type="deal", to_type="contact"``.
Normalisation happens in ``api/services/engage/crm/associations.py`` before any
write — that is what makes the unique index meaningful. Both indexes below make
either lookup direction a single indexed hop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId
from app.enums import AssociationEntity

from ._base import utcnow


class CrmAssociationModel(Document):
    created_by: PyObjectId

    from_type: AssociationEntity
    from_id: PyObjectId
    to_type: AssociationEntity
    to_id: PyObjectId

    # Marks the company shown on a deal's Kanban card. At most one per deal;
    # enforced in the service layer, not by an index.
    is_primary: bool = False
    # Free-form role on a deal<->contact link: "decision_maker", "champion", ...
    role: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    created_by_user_id: Optional[PyObjectId] = None

    class Settings:
        name = "crm_associations"
        indexes = [
            # Uniqueness is only meaningful because writes are normalised to
            # the canonical direction first.
            IndexModel(
                [
                    ("created_by", ASCENDING),
                    ("from_type", ASCENDING),
                    ("from_id", ASCENDING),
                    ("to_type", ASCENDING),
                    ("to_id", ASCENDING),
                ],
                unique=True,
                name="uniq_crm_association_pair",
            ),
            # Reverse lookup: "which deals is this contact on?"
            [("created_by", 1), ("to_type", 1), ("to_id", 1)],
        ]
