"""File schemas. CRM-218.

There is no upload request schema: the payload is multipart, and the only
metadata fields are ``entity_type`` and ``entity_id``, taken as form values.
The filename and content type are NOT accepted from the client — one is
sanitised from the upload and the other is sniffed from the bytes.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId


class FileDetails(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    filename: str
    content_type: str = Field(
        ..., description="Sniffed from the file's bytes, never the client's claim",
    )
    size_bytes: int = 0
    size_label: Optional[str] = Field(
        None, description="Human-readable, so the UI does not re-derive it",
    )
    entity_type: str
    entity_id: PyObjectId
    uploaded_by_id: Optional[PyObjectId] = None
    uploaded_by_name: Optional[str] = None
    created_at: datetime

    # storage_key is deliberately ABSENT. It is the object's location; a client
    # has no use for it and publishing it would invite guessing at neighbours.

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class FileListResponse(BaseModel):
    data: List[FileDetails] = []
    total: int = 0
    status: str = "success"
    message: Optional[str] = ""
