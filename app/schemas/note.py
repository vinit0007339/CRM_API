"""Note schemas. CRM-214.

``body`` arrives as HTML and is sanitised server-side before storage — the
schema deliberately does NOT try to validate markup. A pydantic validator that
attempted it would either duplicate `sanitise.py` or, worse, reject content the
sanitiser would have made safe. Length is the only thing checked here.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId


class NoteCreateRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)
    is_pinned: bool = False
    deal_ids: List[PyObjectId] = []
    company_ids: List[PyObjectId] = []
    contact_ids: List[PyObjectId] = []


class NoteUpdateRequest(BaseModel):
    """Only the body. Attachments are fixed at creation — moving a note between
    records would silently rewrite two timelines.
    """

    body: str = Field(..., min_length=1, max_length=10000)

    model_config = {"extra": "forbid"}


class NoteDetails(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    body: str = Field(..., description="Sanitised HTML. Render with dompurify.")
    preview: Optional[str] = Field(
        None, description="Plain text. Safe for a title, a list row or a "
                          "notification — never render it as HTML.",
    )
    is_pinned: bool = False
    deal_ids: List[PyObjectId] = []
    company_ids: List[PyObjectId] = []
    contact_ids: List[PyObjectId] = []
    author_id: Optional[PyObjectId] = None
    author_name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class NoteListResponse(BaseModel):
    data: List[NoteDetails] = []
    total: int = 0
    status: str = "success"
    message: Optional[str] = ""
