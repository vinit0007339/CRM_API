"""File CRUD. CRM-218."""
from __future__ import annotations

from typing import Any, List, Optional

from app.config import logger as _logger
from app.models.file import CrmFileModel

from app.services.queries import assert_account_scoped

from .base import CrmCrudBase


class CrmFileCrud(CrmCrudBase):
    model = CrmFileModel

    def _filter(self, account_id: Any, entity_type: str, entity_id: Any) -> dict:
        """ONE filter builder for the list and its count, so they cannot drift."""
        return assert_account_scoped({
            "created_by": self.oid(account_id),
            "entity_type": entity_type,
            "entity_id": self.oid(entity_id),
        })

    async def list_for(self, *, account_id: Any, entity_type: str, entity_id: Any,
                       skip: int = 0, limit: int = 50) -> List[CrmFileModel]:
        return await self.list_by(
            self._filter(account_id, entity_type, entity_id),
            sort=[("created_at", -1)], skip=skip, limit=limit,
        )

    async def count_for(self, *, account_id: Any, entity_type: str,
                        entity_id: Any) -> int:
        return await self.count_by(self._filter(account_id, entity_type, entity_id))

    async def by_key(self, *, account_id: Any, storage_key: str
                     ) -> Optional[CrmFileModel]:
        """Look a file up by its storage key, WITHIN the account.

        Scoped deliberately: the dev download route takes a key from the URL,
        and an unscoped lookup there would turn it into an arbitrary-object
        read for anyone who could guess a key.
        """
        acct = self.oid(account_id)
        if not acct or not storage_key:
            return None
        try:
            return await CrmFileModel.find_one({
                "created_by": acct, "storage_key": storage_key,
            })
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return None

    async def for_record(self, *, account_id: Any, entity_type: str,
                         entity_id: Any) -> List[CrmFileModel]:
        """Every file on a record — used when the record itself is deleted."""
        return await self.list_by(
            self._filter(account_id, entity_type, entity_id), limit=500)
