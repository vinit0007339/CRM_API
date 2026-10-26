"""Export schemas. CRM-235.

Ids serialise as ``id``, never ``_id`` — the Phase 1 rule.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId


class ExportRequest(BaseModel):
    """Body of an export request. Every field optional but `separator`."""

    columns: Optional[List[str]] = Field(
        None, description="Internal names, in order. ID and Name are always "
                          "included whatever is asked for — without the ID an "
                          "edited export cannot be re-imported to update the "
                          "records it came from.")
    separator: Literal[",", ";"] = ","
    record_ids: Optional[List[PyObjectId]] = Field(
        None, max_length=10000,
        description="Export exactly these records. Still account-scoped — an "
                    "id arriving from a client is not proof of ownership.")


class ExportDetails(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    entity_type: str
    status: str

    filter_summary: Optional[str] = Field(
        None, description="What was exported, in words — so a file downloaded "
                          "a week later can still say what is in it.")
    columns: List[str] = Field(default_factory=list)
    separator: str = ","

    row_count: int = 0
    file_name: Optional[str] = None
    file_size: int = 0
    is_finished: bool = False

    # Minted at POLL time, never stored. A URL that existed from the moment the
    # file did would sit in the job document, valid, for as long as the row.
    download_url: Optional[str] = None
    expires_in: Optional[int] = None
    is_expired: bool = False

    # storage_key is deliberately ABSENT: it is the object's location, and an
    # export object is the account's entire customer database.

    message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class ExportListResponse(BaseModel):
    data: List[ExportDetails] = Field(default_factory=list)
    total: int = 0
    status: str = "success"
    message: Optional[str] = ""
