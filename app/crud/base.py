"""Shared CRUD behaviour for the CRM collections. CRM-019.

Extends the repo's ``CrudBase`` (which only holds ``db_session`` + a raw motor
collection handle) with the two things every CRM resource needs:

* **id normalisation** — routes and imports hand us strings; Beanie needs
  ObjectIds. Invalid input returns None rather than raising, matching
  ``ListCrud._normalize_id``.
* **tenant-scoped list/count from ONE filter builder.** In the rest of this
  codebase ``list()`` and ``count()`` each rebuild their filter by hand, which
  is how a table shows 20 rows above a "25 results" total. Here both take the
  dict produced by ``api/services/engage/crm/queries.py``, so they cannot
  diverge.

Every query passes through ``assert_account_scoped`` before it reaches Mongo.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Type

from beanie import Document
from bson.errors import InvalidId

from app.config import logger as _logger
from app.crud._platform_base import CrudBase
from app.object_id import PyObjectId

from app.services.queries import assert_account_scoped, paginate


class CrmCrudBase(CrudBase):
    """Base for every CRM CRUD class. Subclasses set ``model``."""

    model: Type[Document] = None  # type: ignore[assignment]

    def __init__(self, db_session):
        super().__init__(db_session)
        try:
            self.collection = db_session[self.model.Settings.name]  # type: ignore[index]
        except (KeyError, TypeError, AttributeError):
            self.collection = None

    # ── ids ──────────────────────────────────────────────────────────────────
    @staticmethod
    def oid(value: Any) -> Optional[PyObjectId]:
        """Coerce to PyObjectId, or None if it isn't one. Never raises."""
        if value is None:
            return None
        if isinstance(value, PyObjectId):
            return value
        try:
            return PyObjectId(value)
        except (InvalidId, TypeError, ValueError):
            return None

    @classmethod
    def oids(cls, values: Optional[Sequence[Any]]) -> List[PyObjectId]:
        """Coerce a list, dropping anything invalid."""
        if not values:
            return []
        return [o for o in (cls.oid(v) for v in values) if o is not None]

    # ── reads ────────────────────────────────────────────────────────────────
    async def get_scoped(self, account_id: Any, record_id: Any) -> Optional[Document]:
        """Fetch by id **within the account**.

        Never use ``Model.get(id)`` directly in a route: it would happily return
        another account's record to anyone who guessed an ObjectId.
        """
        rid, acct = self.oid(record_id), self.oid(account_id)
        if not rid or not acct:
            return None
        try:
            return await self.model.find_one({"_id": rid, "created_by": acct})
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return None

    async def list_by(
        self,
        filters: Dict[str, Any],
        *,
        sort: Optional[List[tuple]] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> List[Document]:
        assert_account_scoped(filters)
        skip, limit = paginate(skip, limit)
        try:
            cursor = self.model.find(filters)
            if sort:
                cursor = cursor.sort(sort)
            return await cursor.skip(skip).limit(limit).to_list()
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return []

    async def count_by(self, filters: Dict[str, Any]) -> int:
        """Takes the SAME dict as list_by — that is the whole point."""
        assert_account_scoped(filters)
        try:
            return await self.model.find(filters).count()
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return 0

    async def exists(self, filters: Dict[str, Any]) -> bool:
        assert_account_scoped(filters)
        try:
            return await self.model.find_one(filters) is not None
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False

    # ── writes ───────────────────────────────────────────────────────────────
    async def insert(self, doc: Document) -> Optional[Document]:
        try:
            return await doc.insert()
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            raise

    async def update_scoped(
        self, account_id: Any, record_id: Any, changes: Dict[str, Any]
    ) -> Optional[Document]:
        """Apply a ``$set`` within the account. Returns the updated document."""
        doc = await self.get_scoped(account_id, record_id)
        if doc is None:
            return None
        try:
            for key, value in changes.items():
                setattr(doc, key, value)
            await doc.save()
            return doc
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            raise

    async def delete_scoped(self, account_id: Any, record_id: Any) -> bool:
        doc = await self.get_scoped(account_id, record_id)
        if doc is None:
            return False
        try:
            await doc.delete()
            return True
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False

    async def touch(self, account_id: Any, record_id: Any, when: Any) -> None:
        """Bump last_activity_at / updated_at and increment activity_count.

        Mirrors ``activity_recorder.parent_touch`` and is called in the same
        operation as the activity write.
        """
        rid, acct = self.oid(record_id), self.oid(account_id)
        if not rid or not acct or self.collection is None:
            return
        try:
            await self.collection.update_one(
                {"_id": rid, "created_by": acct},
                {"$set": {"last_activity_at": when, "updated_at": when},
                 "$inc": {"activity_count": 1}},
            )
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
