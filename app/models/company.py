"""CompanyModel — the organisation record. CRM-006.

Schema strategy is a hybrid, not Brevo's pure EAV bag. Fields we filter, sort,
aggregate or index on are typed columns; anything user-defined lands in
``custom_fields``, mirroring the existing ``Contact.custom_fields`` pattern.
That keeps the list and board queries index-served while still allowing
per-account extensibility.

TENANCY: ``created_by`` is the account id (the owner user's ``_id``). Every
query must filter on it as the leading index key — there is no Account or
Workspace collection in this codebase; see the CRM plan §4.2.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import BaseModel, Field, model_validator

from app.object_id import PyObjectId

from ._base import utcnow


class CompanyAddress(BaseModel):
    """Embedded postal address."""

    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class CompanyModel(Document):
    # ── tenancy ──────────────────────────────────────────────────────────────
    created_by: PyObjectId
    # Reserved, never read. Present from day one so that if multi-workspace is
    # ever revived the migration is a backfill rather than a schema change.
    # Do NOT filter on this — see the CRM plan §4.2 and risk R3.
    workspace_id: Optional[PyObjectId] = None

    # ── core attributes ──────────────────────────────────────────────────────
    name: str
    # Normalised lowercase, no scheme/www/port/path. Indexed but deliberately
    # NOT unique: duplicate detection warns and offers a merge rather than
    # blocking creation, because subsidiaries, franchises and regional entities
    # legitimately share a parent domain (CRM plan §3.3).
    domain: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = None            # value from crm_settings.industries
    employee_count: Optional[int] = None
    annual_revenue: Optional[float] = None
    currency: Optional[str] = None            # ISO-4217; defaults from crm_settings
    phone: Optional[str] = None               # string, not a number — it's an identifier
    phone_country_code: Optional[str] = None
    address: Optional[CompanyAddress] = None
    description: Optional[str] = None

    # ── ownership & classification ───────────────────────────────────────────
    owner_id: Optional[PyObjectId] = None     # a user in this account
    tag_ids: List[PyObjectId] = Field(default_factory=list)   # reuses `tags`

    # ── extensibility ────────────────────────────────────────────────────────
    custom_fields: Dict[str, Any] = Field(default_factory=dict)  # by internal_name

    # Lowercased, legal-suffix-stripped form of `name`, kept so duplicate
    # detection can be an INDEXED EQUALITY LOOKUP instead of an unindexed
    # substring scan. Before this existed the candidate query searched the
    # normalised needle as a substring of the stored name, which cannot match
    # "A.C.M.E. Ltd" against "Acme Limited" — the exact pair name-based
    # detection exists to catch — and scanned the collection to fail.
    #
    # Redundant with `name`, and therefore a drift risk: written in exactly one
    # place (`dedupe.derive_name_fields`), re-derived on construction by the
    # validator below, and re-checked by
    # `scripts/backfill_company_name_normalised.py`.
    name_normalised: Optional[str] = None

    # ── denormalised rollups ─────────────────────────────────────────────────
    # Display only. Never read by business logic, so drift cannot cause
    # incorrect behaviour; a nightly job reconciles them.
    contact_count: int = 0
    deal_count: int = 0
    open_deal_value: float = 0.0
    won_deal_value: float = 0.0
    # CRM-224. Same rule as the rest of this block: display only, maintained by
    # $inc from the task routes, reconciled by
    # scripts/reconcile_crm_task_counters.py.
    open_task_count: int = 0
    overdue_task_count: int = 0
    next_activity_at: Optional[datetime] = None

    # ── merge state (CRM-239 / CRM-240) ──────────────────────────────────────
    # Set on the LOSER of a merge. `merge_snapshot` holds both records as they
    # were plus the ids of everything that moved, which is what makes the merge
    # reversible — and is why unmerge can put back exactly what the merge took
    # rather than everything now pointing at the survivor.
    merged_into_id: Optional[PyObjectId] = None
    merged_at: Optional[datetime] = None
    merge_snapshot: Optional[Dict[str, Any]] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    source: Optional[str] = None              # CrmRecordSource value
    is_archived: bool = False                 # soft delete
    last_activity_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by_user_id: Optional[PyObjectId] = None   # the human, vs the account

    @model_validator(mode="after")
    def _derive_name_normalised(self):
        # Covers every construction path, including ones written later. Direct
        # attribute assignment does NOT re-run this (pydantic only validates on
        # assignment when asked to), which is why the write paths call
        # `dedupe.derive_name_fields` explicitly as well.
        from app.services.dedupe import normalize_company_name

        expected = normalize_company_name(self.name) or None
        if self.name_normalised != expected:
            object.__setattr__(self, "name_normalised", expected)
        return self

    class Settings:
        name = "companies"
        indexes = [
            [("created_by", 1), ("created_at", -1)],          # default list order
            [("created_by", 1), ("name", 1)],                 # name search / sort
            [("created_by", 1), ("domain", 1)],               # dedupe lookups
            # Name-based dedupe, as an equality lookup rather than a scan.
            [("created_by", 1), ("name_normalised", 1)],
            # The 30-day sweep: losers whose window has closed.
            [("created_by", 1), ("merged_at", 1)],
            [("created_by", 1), ("owner_id", 1)],             # "my companies"
            [("created_by", 1), ("is_archived", 1), ("updated_at", -1)],
        ]
