"""CrmSettingsModel — one document per account. CRM-011.

Holds the per-account CRM configuration that isn't big enough to deserve its
own collection: currency, the industry picklist, lost reasons, task types and
the auto-linking behaviour.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class LostReason(BaseModel):
    """An entry in the account's lost-reason picklist.

    Soft-retired via ``is_active`` rather than deleted, so historical deals
    keep resolving their reason label.
    """

    id: PyObjectId = Field(default_factory=PyObjectId)
    label: str
    order: int = 0
    is_active: bool = True


class CrmSettingsModel(Document):
    created_by: PyObjectId                      # unique — one per account

    currency: str = "INR"

    # An IANA zone name. Every "today"/"overdue" boundary in the task module is
    # computed in it — see api/services/engage/crm/task_rules.py. The default
    # matches what the rest of the app already hard-codes (formatDateToIST).
    timezone: str = "Asia/Kolkata"
    industries: List[str] = Field(default_factory=list)
    lost_reasons: List[LostReason] = Field(default_factory=list)
    task_types: List[str] = Field(default_factory=list)

    # On contact create (or email change), find-or-create a company matching
    # the email domain and link them — unless the domain is a free provider.
    auto_link_contacts_by_domain: bool = True
    # True once a human has turned auto-linking on from the settings screen.
    # It exists so the one-time migration that switches the feature off for
    # pre-existing accounts (scripts/migrate_crm_auto_link_default.py) can tell
    # "never chose" from "chose yes" — running it twice would otherwise undo
    # the user's own decision, silently.
    auto_link_opted_in: bool = False

    # ADDS to the built-in free-provider list in auto_link.py, never replaces
    # it: this field defaults to empty, and an empty blocklist would link every
    # @gmail.com contact to one company called Gmail.
    free_email_domains_blocklist: List[str] = Field(default_factory=list)

    # internal_names shown on Kanban cards, in order. Name is always rendered
    # and is not listed here.
    card_display_attributes: List[str] = Field(default_factory=list)
    # CRM-409. Brevo offers showing or hiding the next scheduled activity on a
    # deal card. Defaults to True: `next_activity_at` is the single most useful
    # thing on a card for somebody working a board, so an account that never
    # opens the setting gets it.
    card_show_next_activity: bool = True
    default_pipeline_id: Optional[PyObjectId] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_settings"
        indexes = [
            IndexModel([("created_by", ASCENDING)], unique=True,
                       name="uniq_crm_settings_account"),
        ]
