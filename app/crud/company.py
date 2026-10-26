"""Company CRUD + duplicate detection. CRM-021."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import logger as _logger
from app.models.company import CompanyModel

from app.services import dedupe, queries as qb

from .base import CrmCrudBase


class CompanyCrud(CrmCrudBase):
    model = CompanyModel

    async def list(self, *, account_id: Any, sort_by: Optional[str] = None,
                   sort_dir: str = "desc", skip: int = 0, limit: int = 50,
                   **filters) -> List[CompanyModel]:
        return await self.list_by(
            qb.company_filter(account_id=self.oid(account_id), **filters),
            sort=qb.company_sort(sort_by, sort_dir), skip=skip, limit=limit,
        )

    async def count(self, *, account_id: Any, **filters) -> int:
        # Same builder, same arguments as list() — they cannot diverge.
        return await self.count_by(
            qb.company_filter(account_id=self.oid(account_id), **filters)
        )

    async def find_duplicates(
        self, *, account_id: Any, name: Optional[str] = None,
        domain: Optional[str] = None, exclude_id: Any = None,
        min_confidence: str = dedupe.LOW, candidate_limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Candidate lookup + scoring.

        The DB does the cheap indexed narrowing on ``(created_by, domain)``;
        ``dedupe.py`` does the scoring. Returns [] when there is nothing to
        match on, rather than scanning the whole account.
        """
        normalised = dedupe.normalize_domain(domain)
        # The candidate query must be a SUPERSET of what the scorer will match.
        # The scorer compares *normalised* names ("Acme Pvt Ltd" and "ACME
        # Limited" both reduce to "acme"), so searching the raw name as a
        # substring would return nothing for exactly the pairs we care about
        # and name-based detection would silently never fire. Search on the
        # normalised form, which is a substring of the stored name.
        needle = dedupe.normalize_company_name(name) or None
        if not normalised and not needle:
            return []
        try:
            f = qb.company_dedupe_filter(
                account_id=self.oid(account_id), domain=normalised, name=needle
            )
        except ValueError:
            return []
        candidates = await self.list_by(f, limit=candidate_limit)
        return dedupe.find_duplicates(
            name, normalised,
            [{"id": c.id, "name": c.name, "domain": c.domain} for c in candidates],
            exclude_id=exclude_id, min_confidence=min_confidence,
        )

    async def rename_fanout(self, *, account_id: Any, company_id: Any, new_name: str) -> int:
        """Refresh the denormalised ``primary_company_name`` on this company's deals.

        Bounded by the company's deal count. The counter is display-only, so a
        failure here degrades a label — it never affects behaviour.
        """
        acct, cid = self.oid(account_id), self.oid(company_id)
        if not acct or not cid or self.db_session is None:
            return 0
        try:
            res = await self.db_session["deals"].update_many(
                {"created_by": acct, "primary_company_id": cid},
                {"$set": {"primary_company_name": new_name}},
            )
            return int(getattr(res, "modified_count", 0) or 0)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return 0
