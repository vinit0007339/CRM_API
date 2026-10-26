"""Attribute request/response schemas. CRM-207.

`internal_name` and `attribute_type` are absent from ``AttributeUpdateRequest``
by design, and the model is ``extra="forbid"`` — so a client that sends one gets
a 422 rather than a silent no-op. ``attributes.assert_mutable`` is the second
layer, for callers that reach the service without going through this schema.
Two layers because the cost of the guard failing is unbounded and silent: every
value stored under the old key is orphaned with no error and no way to find it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId
from app.enums import AttributeType, CrmObjectType


class AttributeOptionSchema(BaseModel):
    key: Optional[str] = Field(
        None,
        description="Stored on the record. Generated from the label if omitted, "
                    "then IMMUTABLE — rekeying orphans every stored value.",
    )
    label: str = Field(..., min_length=1, max_length=60)
    color: Optional[str] = None


class AttributeCreateRequest(BaseModel):
    object_type: CrmObjectType
    label: str = Field(..., min_length=1, max_length=50)
    attribute_type: AttributeType
    description: Optional[str] = Field(None, max_length=500)
    options: List[AttributeOptionSchema] = []
    is_required: bool = False
    is_readonly: bool = False
    show_on_form: bool = True
    show_on_card: bool = False
    group: Optional[str] = Field(None, max_length=50)
    # internal_name is NOT accepted: it is derived from the label, so there is
    # no way for a client to pick a key that collides or that is unstorable.


class AttributeUpdateRequest(BaseModel):
    """Everything that may change after creation.

    Note what is missing: `internal_name`, `attribute_type`, `object_type`,
    `is_system`.
    """

    label: Optional[str] = Field(None, min_length=1, max_length=50)
    description: Optional[str] = Field(None, max_length=500)
    options: Optional[List[AttributeOptionSchema]] = None
    is_required: Optional[bool] = None
    is_readonly: Optional[bool] = None
    show_on_form: Optional[bool] = None
    show_on_card: Optional[bool] = None
    form_order: Optional[int] = Field(None, ge=0)
    group: Optional[str] = Field(None, max_length=50)

    model_config = {"extra": "forbid"}


class AttributeReorderRequest(BaseModel):
    object_type: CrmObjectType
    attribute_ids: List[PyObjectId] = Field(
        ..., description="Ordered. Anything omitted keeps its relative place after these.",
    )


class AttributeDetails(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    object_type: CrmObjectType
    internal_name: str
    label: str
    attribute_type: AttributeType
    description: Optional[str] = None
    options: List[AttributeOptionSchema] = []
    is_required: bool = False
    is_readonly: bool = False
    is_system: bool = False
    show_on_form: bool = True
    show_on_card: bool = False
    form_order: int = 0
    group: Optional[str] = None
    # Populated on the detail response only — how many records hold a value.
    # Drives the delete confirmation, so it must be the server's count.
    value_count: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class AttributeListResponse(BaseModel):
    data: List[AttributeDetails] = []
    total: int = 0
    status: str = "success"
    message: Optional[str] = ""
