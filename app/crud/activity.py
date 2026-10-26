"""Activity timeline CRUD. CRM-019. Append-only: no update, no delete."""
from __future__ import annotations

from typing import Any, Dict, List

from app.config import logger as _logger
from app.models.activity import CrmActivityModel

from app.services import queries as qb

from .base import CrmCrudBase


class CrmActivityCrud(CrmCrudBase):
    model = CrmActivityModel

    async def list(self, *, account_id: Any, skip: int = 0, limit: int = 30,
                   **filters) -> List[CrmActivityModel]:
        return await self.list_by(
            qb.activity_filter(account_id=self.oid(account_id), **filters),
            sort=qb.activity_sort(), skip=skip, limit=limit,
        )

    async def count(self, *, account_id: Any, **filters) -> int:
        return await self.count_by(
            qb.activity_filter(account_id=self.oid(account_id), **filters)
        )

    async def write(self, payload: Dict[str, Any]) -> None:
        """Persist one activity. The writer callable for activity_recorder.record()."""
        try:
            await CrmActivityModel(**payload).insert()
        except Exception as ex:  # pylint: disable=broad-except
            # A timeline entry must never fail the user's actual action.
            _logger.exception(ex)

    async def write_many(self, payloads: List[Dict[str, Any]]) -> None:
        if not payloads:
            return
        try:
            await CrmActivityModel.insert_many(
                [CrmActivityModel(**p) for p in payloads]
            )
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
