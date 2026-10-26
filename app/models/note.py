"""CrmNoteModel — free-text notes on companies, deals and contacts. CRM-214.

Per requirements §6.7.

LINKING: direct id arrays, not ``crm_associations``. Deliberately inconsistent
with companies↔deals↔contacts, and for the same reason tasks are: a note is a
leaf object. Nothing traverses *from* a note at scale, so the join collection's
reverse-lookup index would earn nothing, while array-membership indexes serve
"notes on this deal, newest first" in one hop.

A note may hang off several records at once — a call covering two deals is one
note, not two. Deleting one of those records does not delete the note; it stops
appearing there.

``body`` is sanitised HTML. It is sanitised on the way IN, by
``api/services/engage/crm/sanitise.py``, never on the way out — see that
module's docstring for why that distinction matters.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class CrmNoteModel(Document):
    created_by: PyObjectId                      # account tenancy
    body: str                                   # sanitised HTML, 1..10000

    is_pinned: bool = False

    company_ids: List[PyObjectId] = Field(default_factory=list)
    deal_ids: List[PyObjectId] = Field(default_factory=list)
    contact_ids: List[PyObjectId] = Field(default_factory=list)

    author_id: PyObjectId
    author_name: Optional[str] = None           # denormalised for the timeline

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_notes"
        indexes = [
            # "notes on this record, newest first" — one indexed hop each.
            IndexModel([("created_by", ASCENDING), ("deal_ids", ASCENDING),
                        ("created_at", DESCENDING)], name="notes_by_deal"),
            IndexModel([("created_by", ASCENDING), ("company_ids", ASCENDING),
                        ("created_at", DESCENDING)], name="notes_by_company"),
            IndexModel([("created_by", ASCENDING), ("contact_ids", ASCENDING),
                        ("created_at", DESCENDING)], name="notes_by_contact"),
            # Pinned notes sort first and are read on every note list, so the
            # sort is index-served rather than done in memory per request.
            IndexModel([("created_by", ASCENDING), ("is_pinned", DESCENDING),
                        ("created_at", DESCENDING)], name="notes_pinned"),
        ]
