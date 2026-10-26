"""CrmAttributeModel — describes both custom and core CRM fields. CRM-012.

Drives dynamic form rendering, the Kanban card configuration, the column
customiser and import validation, so one UI component can render core and
custom fields identically.

Core typed fields are seeded as rows with ``is_system=True``. Writes to a
system attribute's value route to the typed model column; writes to any other
attribute land in the parent's ``custom_fields`` dict. That is what lets an
account mark a *core* field required-on-create without a code change.

``internal_name`` is IMMUTABLE after creation — renaming it would orphan every
value already stored under the old key in ``custom_fields``. ``label`` is freely
editable. The route layer enforces this (CRM-024); the model can't.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId
from app.enums import AttributeType, CrmObjectType

from ._base import utcnow


class CrmAttributeOption(BaseModel):
    """A choice for single_select / multi_select attributes."""

    key: str
    label: str
    color: Optional[str] = None


class CrmAttributeModel(Document):
    created_by: PyObjectId
    object_type: CrmObjectType

    # Slug. Unique per (account, object_type). Immutable — see module docstring.
    internal_name: str
    label: str                                  # <= 50 chars, enforced in schema
    attribute_type: AttributeType
    description: Optional[str] = None
    options: List[CrmAttributeOption] = Field(default_factory=list)

    is_required: bool = False
    is_readonly: bool = False
    # True for seeded rows describing the typed core columns (name, domain,
    # amount, ...). System attributes can be reordered, relabelled and marked
    # required, but never deleted and never retyped.
    is_system: bool = False

    show_on_form: bool = True
    show_on_card: bool = False                  # Kanban card display
    form_order: int = 0
    group: Optional[str] = None                 # detail-page attribute group

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_attributes"
        indexes = [
            IndexModel(
                [("created_by", ASCENDING), ("object_type", ASCENDING),
                 ("internal_name", ASCENDING)],
                unique=True,
                name="uniq_crm_attribute_internal_name",
            ),
            [("created_by", 1), ("object_type", 1), ("form_order", 1)],
        ]
