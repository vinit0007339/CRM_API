"""Pipeline CRUD. CRM-019.

Stage mutations are pipeline-document rewrites — validated by
``stage_rules.py`` before they get here.
"""
from __future__ import annotations

from typing import Any, List, Optional

from app.config import logger as _logger
from app.models.pipeline import PipelineModel

from app.services.queries import assert_account_scoped

from .base import CrmCrudBase


class PipelineCrud(CrmCrudBase):
    model = PipelineModel

    async def list(self, *, account_id: Any, include_archived: bool = False
                   ) -> List[PipelineModel]:
        f = {"created_by": self.oid(account_id)}
        if not include_archived:
            f["is_archived"] = False
        return await self.list_by(assert_account_scoped(f),
                                  sort=[("created_at", 1)], limit=200)

    async def get_default(self, *, account_id: Any) -> Optional[PipelineModel]:
        acct = self.oid(account_id)
        if not acct:
            return None
        try:
            return await PipelineModel.find_one(
                {"created_by": acct, "is_default": True, "is_archived": False}
            )
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return None

    async def clear_default(self, *, account_id: Any, except_id: Any = None) -> None:
        """Exactly one default per account — call before setting a new one."""
        acct = self.oid(account_id)
        if not acct or self.collection is None:
            return
        f = {"created_by": acct, "is_default": True}
        keep = self.oid(except_id)
        if keep:
            f["_id"] = {"$ne": keep}
        try:
            await self.collection.update_many(f, {"$set": {"is_default": False}})
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)

    async def count_deals(self, *, account_id: Any, pipeline_id: Any,
                          stage_id: Any = None) -> int:
        """Deal count for a pipeline or one of its stages.

        Gates pipeline/stage deletion; the counts appear verbatim in the
        refusal messages built by ``stage_rules.py``.
        """
        acct, pid = self.oid(account_id), self.oid(pipeline_id)
        if not acct or not pid or self.db_session is None:
            return 0
        f = {"created_by": acct, "pipeline_id": pid}
        sid = self.oid(stage_id)
        if sid:
            f["stage_id"] = sid
        try:
            return int(await self.db_session["deals"].count_documents(f))
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return 0
