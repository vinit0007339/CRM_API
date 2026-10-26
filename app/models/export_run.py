"""CrmExportModel — one export job. CRM-235.

The job document. The UI polls it; the worker writes it; and when it is done the
poll mints a short-lived signed URL from ``storage_key``.

⚠️ **AN EXPORT FILE IS THE ACCOUNT'S ENTIRE CUSTOMER DATABASE IN ONE OBJECT.**
That is a bigger concentration of data than any single record, so:

* the stored object is private and reached only through a signed URL minted at
  poll time, never at write time — a URL that existed from the moment the file
  did would be sitting in this document, valid, for as long as the row lives;
* ``expires_at`` exists so the sweep can delete the object. An export nobody
  deletes is a copy of the customer list that outlives the reason it was made,
  and there is no version of "we kept it forever" that reads well in a breach
  report.

``storage_key`` never leaves the server — see the schema.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow

EXPORT_STATUSES = ("pending", "running", "completed", "failed")
TERMINAL_STATUSES = ("completed", "failed")

# How long an export file lives before the sweep removes it.
EXPORT_TTL_DAYS = 7


class CrmExportModel(Document):
    created_by: PyObjectId                      # account tenancy

    entity_type: str                            # company | deal
    status: str = "pending"

    # The filters the export was taken with, kept so a file downloaded a week
    # later can still say what is in it. Stored as the plain values the list
    # endpoint received.
    filters: Dict[str, Any] = Field(default_factory=dict)
    filter_summary: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    separator: str = ","

    row_count: int = 0
    total_rows: int = 0
    file_name: Optional[str] = None
    file_size: int = 0

    # Private object location. NEVER returned to a client.
    storage_key: Optional[str] = None

    message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # When the sweep may delete the object. See the module docstring.
    expires_at: Optional[datetime] = None

    created_by_user_id: Optional[PyObjectId] = None
    created_by_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_exports"
        indexes = [
            IndexModel([("created_by", ASCENDING), ("created_at", DESCENDING)],
                       name="exports_by_account"),
            # The retention sweep: every job whose file is past its date.
            IndexModel([("expires_at", ASCENDING)], name="exports_expiry",
                       sparse=True),
        ]
