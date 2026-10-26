"""CRM module enums — the single source of truth for both layers.

Imported by the Beanie models (``app/db/models/main/crm/``) *and* the API
schemas (``api/schemas/main/crm/``) so the two can never drift. Stdlib-only,
no DB or pydantic dependency.
"""
from __future__ import annotations

from enum import Enum


class StageType(str, Enum):
    """A pipeline stage's outcome semantics.

    Brevo treats Won and Lost as ordinary stages and reasons about them by
    name. We type them instead, so reporting and automation never have to
    string-match a user-renameable label.
    """

    OPEN = "open"
    WON = "won"
    LOST = "lost"


class DealStatus(str, Enum):
    """Denormalised onto the deal from its stage's type.

    Reporting, filtering and automation all need won/lost without a join to
    the pipeline document. Written in the same operation as any stage change,
    in the service layer — never independently.
    """

    OPEN = "open"
    WON = "won"
    LOST = "lost"


class CrmObjectType(str, Enum):
    """Objects that support custom attributes."""

    COMPANY = "company"
    DEAL = "deal"
    CONTACT = "contact"


class AssociationEntity(str, Enum):
    """Participants in ``crm_associations``.

    Ordering here is meaningful: it defines the canonical direction a pair is
    stored in (see ``api/services/engage/crm/associations.py``). Reordering
    these members changes the storage direction and would orphan existing
    rows — don't.
    """

    COMPANY = "company"
    DEAL = "deal"
    CONTACT = "contact"


class AttributeType(str, Enum):
    """13 custom-attribute types.

    Brevo ships 7 (text, number, date, boolean, single-select, multi-choice,
    user) and forces URLs and phone numbers into Text/Number, which loses
    validation and click-to-action affordances. We add textarea, datetime,
    url, email, phone and currency.
    """

    TEXT = "text"
    TEXTAREA = "textarea"
    NUMBER = "number"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    USER = "user"
    URL = "url"
    EMAIL = "email"
    PHONE = "phone"
    CURRENCY = "currency"

    @classmethod
    def requires_options(cls) -> "set[AttributeType]":
        return {cls.SINGLE_SELECT, cls.MULTI_SELECT}


class ActivityType(str, Enum):
    """Timeline entry kinds. Append new members; never rename existing ones —
    stored rows carry the string value.
    """

    CREATED = "created"
    UPDATED = "updated"
    STAGE_CHANGED = "stage_changed"
    WON = "won"
    LOST = "lost"
    REOPENED = "reopened"
    OWNER_CHANGED = "owner_changed"
    NOTE_ADDED = "note_added"
    NOTE_UPDATED = "note_updated"
    NOTE_DELETED = "note_deleted"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    FILE_UPLOADED = "file_uploaded"
    FILE_DELETED = "file_deleted"
    CONTACT_LINKED = "contact_linked"
    CONTACT_UNLINKED = "contact_unlinked"
    COMPANY_LINKED = "company_linked"
    COMPANY_UNLINKED = "company_unlinked"
    DEAL_LINKED = "deal_linked"
    DEAL_UNLINKED = "deal_unlinked"
    IMPORTED = "imported"
    MERGED = "merged"
    AUTOMATION = "automation"
    EMAIL_SENT = "email_sent"


class ActivitySource(str, Enum):
    UI = "ui"
    API = "api"
    IMPORT = "import"
    AUTOMATION = "automation"
    SYSTEM = "system"


class CrmRecordSource(str, Enum):
    """How a company or deal came into existence."""

    MANUAL = "manual"
    IMPORT = "import"
    API = "api"
    AUTO_DOMAIN = "auto_domain"
    AUTOMATION = "automation"


class ImportStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
