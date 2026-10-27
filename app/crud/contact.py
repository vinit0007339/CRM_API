"""CRM contact CRUD. E25."""
from __future__ import annotations

from typing import Any, List, Optional

from app.config import logger as _logger, settings
from app.models.contact import CrmContactModel

from app.services import queries as qb

from .base import CrmCrudBase


class CrmContactCrud(CrmCrudBase):
    model = CrmContactModel

    async def list(self, *, account_id: Any, sort_by: Optional[str] = None,
                   sort_dir: str = "desc", skip: int = 0, limit: int = 50,
                   **filters) -> List[CrmContactModel]:
        return await self.list_by(
            qb.contact_filter(account_id=self.oid(account_id), **filters),
            sort=qb.contact_sort(sort_by, sort_dir), skip=skip, limit=limit,
        )

    async def count(self, *, account_id: Any, **filters) -> int:
        # Same builder, same arguments as list() — they cannot diverge.
        return await self.count_by(
            qb.contact_filter(account_id=self.oid(account_id), **filters))

    async def by_email(self, *, account_id: Any,
                       email: Optional[str]) -> Optional[CrmContactModel]:
        """The account's contact with this address, or None.

        ⚠️ `email` MUST ALREADY BE NORMALISED (`contact_rules.normalize_email`).
        This is the read side of `uniq_crm_contact_email`, and a raw address
        would miss the stored lowercased one — so the duplicate check would
        pass, the INSERT would then fail on the index, and a friendly "you
        already have this contact" would arrive as a 500.

        Archived contacts are INCLUDED: the index does not care, so neither can
        this. Excluding them would report a free address and then collide.
        """
        if not email:
            return None
        return await self.model.find_one(
            qb.contact_filter(account_id=self.oid(account_id), email=email,
                              is_archived=None))

    async def rename_company_fanout(self, *, account_id: Any, company_id: Any,
                                    new_name: str) -> int:
        """Refresh the denormalised ``primary_company_name``.

        Display-only, exactly like `CompanyCrud.rename_fanout` for deals: a
        failure here degrades a label and never affects behaviour.
        """
        acct, cid = self.oid(account_id), self.oid(company_id)
        if not acct or not cid or self.db_session is None:
            return 0
        try:
            res = await self.db_session["crm_contacts"].update_many(
                {"created_by": acct, "primary_company_id": cid},
                {"$set": {"primary_company_name": new_name}},
            )
            return int(getattr(res, "modified_count", 0) or 0)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return 0
