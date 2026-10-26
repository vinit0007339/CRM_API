"""Note CRUD. CRM-214."""
from __future__ import annotations

from typing import Any, List

from app.models.note import CrmNoteModel

from app.services.queries import assert_account_scoped

from .base import CrmCrudBase

# Which array field holds the ids for each parent kind.
_FIELD = {"deal": "deal_ids", "company": "company_ids", "contact": "contact_ids"}


class CrmNoteCrud(CrmCrudBase):
    model = CrmNoteModel

    def _filter(self, account_id: Any, entity_type: str, entity_id: Any) -> dict:
        """ONE filter builder, shared by list_for and count_for.

        Same rule as the deal board: a list and its count must be built from
        the same dict, or the header disagrees with the rows under it.
        """
        field = _FIELD.get(entity_type)
        if not field:
            raise ValueError(f"unknown note parent: {entity_type}")
        return assert_account_scoped({
            "created_by": self.oid(account_id),
            field: self.oid(entity_id),
        })

    async def list_for(self, *, account_id: Any, entity_type: str, entity_id: Any,
                       skip: int = 0, limit: int = 30) -> List[CrmNoteModel]:
        """Pinned first, then newest first — matching the `notes_pinned` index."""
        return await self.list_by(
            self._filter(account_id, entity_type, entity_id),
            sort=[("is_pinned", -1), ("created_at", -1)],
            skip=skip, limit=limit,
        )

    async def count_for(self, *, account_id: Any, entity_type: str,
                        entity_id: Any) -> int:
        return await self.count_by(self._filter(account_id, entity_type, entity_id))
