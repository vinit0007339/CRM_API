"""CrmImportModel — one import run. CRM-229 / CRM-230 / CRM-231.

The progress document. The UI polls it; the worker writes it; and
``cancel_requested`` on it is how a cancel actually reaches a job already
running, since the worker is a background task with no other channel to it.

ERRORS ARE CAPPED IN THIS DOCUMENT, COUNTERS ARE NOT. A 100k-row import with
100k bad rows would blow past Mongo's 16 MB document limit and the whole import
record would fail to save — losing the counters too, which are the part the
user actually needs. So ``failed_count`` stays exact while the samples stop at
``import_rules.MAX_ERROR_ROWS`` and ``errors_truncated`` says so. The existing
``ListImportModel`` learned this the same way.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow

# pending → running → one of completed | completed_with_errors | failed | cancelled
IMPORT_STATUSES = ("pending", "running", "completed", "completed_with_errors",
                   "failed", "cancelled")
TERMINAL_STATUSES = ("completed", "completed_with_errors", "failed", "cancelled")


class CrmImportModel(Document):
    created_by: PyObjectId                      # account tenancy

    entity_type: str                            # company | deal
    file_name: Optional[str] = None
    file_size: int = 0

    # What the sniffer decided. Surfaced in the UI: "Detected: UTF-16,
    # tab-separated" is the fastest way for someone to notice they exported the
    # wrong thing, and the fastest way for us to diagnose a garbled import.
    file_format: Optional[str] = None
    encoding: Optional[str] = None
    delimiter: Optional[str] = None

    headers: List[str] = Field(default_factory=list)
    # Positional, one entry per column, None for "don't import this column".
    # Positional rather than keyed by header text because files with two
    # identically-named columns are common.
    mapping: List[Optional[str]] = Field(default_factory=list)
    options: Dict[str, Any] = Field(default_factory=dict)

    status: str = "pending"

    total_rows: int = 0
    processed_rows: int = 0
    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    # Companies conjured while linking deals. Counted separately so the summary
    # can be honest about a side effect the user did not explicitly ask for.
    linked_created_count: int = 0

    errors: List[Dict[str, Any]] = Field(default_factory=list)
    errors_truncated: bool = False

    # The only channel a cancel has into a running background task. The worker
    # re-reads this between batches.
    cancel_requested: bool = False
    # What was already written when the cancel landed. An import is not
    # transactional, so "cancelled" without this number leaves the user unable
    # to tell whether to re-run it.
    committed_at_cancel: int = 0

    message: Optional[str] = None

    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_by_user_id: Optional[PyObjectId] = None
    created_by_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_imports"
        indexes = [
            IndexModel([("created_by", ASCENDING), ("created_at", DESCENDING)],
                       name="imports_by_account"),
            IndexModel([("created_by", ASCENDING), ("entity_type", ASCENDING),
                        ("status", ASCENDING)], name="imports_by_status"),
        ]
