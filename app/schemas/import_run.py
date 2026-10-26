"""Import schemas. CRM-229 / CRM-230 / CRM-231.

Ids serialise as ``id``, never ``_id`` — the Phase 1 rule.

There is no request schema for the upload itself: it is multipart, and the
mapping and options arrive as JSON strings in form fields because a multipart
body cannot carry nested JSON any other way.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId


class ImportColumn(BaseModel):
    """One column of the uploaded file, as the map screen sees it."""

    index: int
    header: str
    samples: List[str] = Field(
        default_factory=list,
        description="Example values. 'Field_7' says nothing; "
                    "'Field_7 · acme.com, globex.io' is obviously the domain.",
    )
    suggested: Optional[str] = Field(
        None, description="Suggested target attribute, or null. Exact matches "
                          "only — a plausible-but-wrong guess is worse than "
                          "none, because the user trusts a pre-filled dropdown.",
    )


class ImportPreview(BaseModel):
    entity_type: str
    file_name: Optional[str] = None
    file_size: int = 0

    # What the sniffer decided, surfaced so the user can see the file was
    # understood before committing to a run of 9,000 rows.
    file_format: Optional[str] = None
    encoding: Optional[str] = None
    delimiter: Optional[str] = None

    columns: List[ImportColumn] = Field(default_factory=list)
    rows: List[List[str]] = Field(default_factory=list)
    total_preview_rows: int = 0
    truncated: bool = Field(
        False, description="True when the file has more rows than the preview shows",
    )


class ImportOptions(BaseModel):
    update_existing: bool = False
    dedupe_key: Optional[Literal["domain", "name", "company_id"]] = None
    unset_empty_attributes: bool = False
    link_entities: bool = True
    default_owner_id: Optional[PyObjectId] = None

    @model_validator(mode="after")
    def update_existing_needs_a_key(self):
        # Without a key there is nothing to match on, so "update existing"
        # would silently create everything — the exact opposite of what was
        # asked for, and invisible until someone counts the records.
        if self.update_existing and not self.dedupe_key:
            raise ValueError(
                "Choose which column identifies an existing record before "
                "turning on 'update existing'.")
        return self


class ImportDetails(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    entity_type: str
    status: str
    file_name: Optional[str] = None
    file_size: int = 0
    file_format: Optional[str] = None
    encoding: Optional[str] = None
    delimiter: Optional[str] = None

    total_rows: int = 0
    processed_rows: int = 0
    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    linked_created_count: int = Field(
        0, description="Companies created while linking deals — a side effect "
                       "worth reporting rather than discovering later.",
    )

    percent: int = Field(0, description="0-100, computed server-side")
    is_finished: bool = False

    errors_truncated: bool = False
    error_count: int = 0
    cancel_requested: bool = False
    committed_at_cancel: int = 0

    message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime

    # A handful of examples for the progress screen. The full report is the CSV
    # at /errors — a thousand rows of JSON is not something anyone reads in a
    # browser.
    error_samples: List[Dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class ImportListResponse(BaseModel):
    data: List[ImportDetails] = Field(default_factory=list)
    total: int = 0
    status: str = "success"
    message: Optional[str] = ""
