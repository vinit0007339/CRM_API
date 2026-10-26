"""Pipeline + embedded stage schemas — CRM-004 frozen contract.

Routes: ``/api/v1/crm/pipelines``. Stages are embedded in the pipeline document,
not a separate collection: a pipeline has 3-20 stages and they are always loaded
together, so embedding means the board endpoint reads one document to know its
columns and reordering is a single write. Deals reference ``stage_id`` — the
embedded stage's own ObjectId — which is stable across renames and reorders.

Brevo exposes no pipeline/stage CRUD via API at all (UI only). We do; it costs
almost nothing and makes the module scriptable.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId

from .common import StageType


class StageSchema(BaseModel):
    id: PyObjectId
    name: str
    order: int = Field(..., ge=0, description="0-based; contiguous after every mutation")
    type: StageType = StageType.OPEN
    win_probability: int = Field(
        0, ge=0, le=100, description="Forced to 100 on the won stage, 0 on the lost stage"
    )
    color: Optional[str] = None
    required_attributes: List[str] = Field(
        [], description="internal_names that must be set before a deal may enter this stage"
    )
    rotting_days: Optional[int] = Field(
        None, ge=1, description="Deal is flagged rotting after this many days in the stage"
    )


class StageCreateRequest(BaseModel):
    """New stages are always inserted before Won/Lost."""

    name: str = Field(..., min_length=1, max_length=60)
    win_probability: int = Field(0, ge=0, le=100)
    color: Optional[str] = None
    required_attributes: List[str] = []
    rotting_days: Optional[int] = Field(None, ge=1)
    after_stage_id: Optional[PyObjectId] = None


class StageUpdateRequest(BaseModel):
    """Won/Lost stages accept ``name`` and ``color`` only."""

    name: Optional[str] = Field(None, min_length=1, max_length=60)
    win_probability: Optional[int] = Field(None, ge=0, le=100)
    color: Optional[str] = None
    required_attributes: Optional[List[str]] = None
    rotting_days: Optional[int] = Field(None, ge=1)


class StageReorderRequest(BaseModel):
    stage_ids: List[PyObjectId] = Field(
        ...,
        description="Full ordered list of the OPEN stages. Won/Lost are implicit and pinned last.",
    )


class PipelineCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    is_default: bool = False
    weighted_forecast_enabled: bool = False
    currency: Optional[str] = None
    stages: Optional[List[StageCreateRequest]] = Field(
        None, description="Omit to seed a copy of the default stage set"
    )


class PipelineUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = None
    is_default: Optional[bool] = None
    weighted_forecast_enabled: Optional[bool] = None
    currency: Optional[str] = None
    is_archived: Optional[bool] = None


class PipelineDetails(BaseModel):
    # Reads `_id` from Mongo, writes `id` on the wire. FastAPI serialises
    # response_model with by_alias=True, so without serialization_alias every
    # client would receive `_id` while every screen reads `.id`.
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    name: str
    description: Optional[str] = None
    is_default: bool = False
    is_archived: bool = False
    weighted_forecast_enabled: bool = False
    currency: Optional[str] = None
    stages: List[StageSchema] = []
    deal_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class PipelineListResponse(BaseModel):
    data: List[PipelineDetails] = []
    total: int = 0
    status: str = "success"
    message: Optional[str] = ""
