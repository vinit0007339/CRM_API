"""CrmFileModel — attachment metadata. CRM-218.

The bytes live in object storage; this is the row that points at them and
carries everything the UI needs without touching the bucket.

``storage_key`` is generated, never derived from the uploaded filename — see
``file_rules.storage_key``. ``filename`` is the sanitised display name, put
back into the Content-Disposition header at download time.

``content_type`` is what we SNIFFED, never what the client claimed. It is the
type the file will be served as, so a wrong value here is the whole attack.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class CrmFileModel(Document):
    created_by: PyObjectId                      # account tenancy

    storage_key: str                            # random; the object's location
    filename: str                               # sanitised display name
    content_type: str                           # sniffed, not claimed
    size_bytes: int = 0

    # Files attach to exactly one record. Unlike notes, an attachment shared
    # between two deals would raise a question this feature has no answer for:
    # deleting it from one should not delete it from the other, which means
    # reference counting for no real benefit.
    entity_type: str                            # deal | company | contact
    entity_id: PyObjectId

    uploaded_by_id: Optional[PyObjectId] = None
    uploaded_by_name: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_files"
        indexes = [
            IndexModel([("created_by", ASCENDING), ("entity_type", ASCENDING),
                        ("entity_id", ASCENDING), ("created_at", DESCENDING)],
                       name="files_by_record"),
            # The orphan sweep and the local-dev download both look a file up
            # by its key.
            IndexModel([("created_by", ASCENDING), ("storage_key", ASCENDING)],
                       name="files_by_key"),
        ]
