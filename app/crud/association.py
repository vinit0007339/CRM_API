"""Association CRUD + rollup-counter maintenance. CRM-023.

Every write goes through ``associations.normalize_pair`` first, so the unique
index in ``crm_associations`` actually means something: linking A->B and B->A
produce the same row.

Counters (``contact_count``, ``deal_count``, ``primary_company_name``, ...) are
maintained here. They are display-only and never read by business logic, so
drift degrades a label rather than causing incorrect behaviour; a nightly job
reconciles them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.config import logger as _logger
from app.models.association import CrmAssociationModel

from app.services import queries as qb
from app.services.associations import (
    InvalidAssociation, counterpart, normalize_pair, pair_filter,
)

from .base import CrmCrudBase

_COUNTER_FIELD = {
    ("company", "contact"): "contact_count",
    ("company", "deal"): "deal_count",
    ("deal", "contact"): "contact_count",
}
_COLLECTION = {"company": "companies", "deal": "deals"}


class CrmAssociationCrud(CrmCrudBase):
    model = CrmAssociationModel

    async def link(self, *, account_id: Any, from_type: str, from_id: Any,
                   to_type: str, to_id: Any, is_primary: bool = False,
                   role: Optional[str] = None, actor_user_id: Any = None) -> bool:
        """Create one association. Idempotent: re-linking is a no-op, not an error."""
        acct = self.oid(account_id)
        try:
            ft, fid, tt, tid = normalize_pair(
                from_type, self.oid(from_id), to_type, self.oid(to_id)
            )
        except InvalidAssociation as ex:
            _logger.warning(f"link rejected: {ex}")
            return False
        if not acct or fid is None or tid is None:
            return False

        if await self.exists(pair_filter(acct, ft, fid, tt, tid)):
            return False   # already linked — not an error
        try:
            await CrmAssociationModel(
                created_by=acct, from_type=ft, from_id=fid, to_type=tt, to_id=tid,
                is_primary=is_primary, role=role,
                created_by_user_id=self.oid(actor_user_id),
            ).insert()
        except Exception as ex:  # pylint: disable=broad-except
            # A racing duplicate hits the unique index — same outcome as above.
            _logger.warning(f"link insert failed (likely duplicate): {ex}")
            return False
        await self._bump(acct, ft, fid, tt, tid, delta=1)
        return True

    async def unlink(self, *, account_id: Any, from_type: str, from_id: Any,
                     to_type: str, to_id: Any) -> bool:
        acct = self.oid(account_id)
        try:
            ft, fid, tt, tid = normalize_pair(
                from_type, self.oid(from_id), to_type, self.oid(to_id)
            )
        except InvalidAssociation:
            return False
        if not acct or self.collection is None:
            return False
        try:
            res = await self.collection.delete_one(pair_filter(acct, ft, fid, tt, tid))
            if not int(getattr(res, "deleted_count", 0) or 0):
                return False
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False
        await self._bump(acct, ft, fid, tt, tid, delta=-1)
        return True

    async def list_for(self, *, account_id: Any, entity_type: str, entity_id: Any,
                       skip: int = 0, limit: int = 100) -> List[CrmAssociationModel]:
        return await self.list_by(
            qb.associations_of(account_id=self.oid(account_id),
                               entity_type=entity_type, entity_id=self.oid(entity_id)),
            skip=skip, limit=limit,
        )

    async def counterparts(self, *, account_id: Any, entity_type: str, entity_id: Any,
                           want_type: str) -> List[Any]:
        """Ids of everything of ``want_type`` linked to this record."""
        eid = self.oid(entity_id)
        rows = await self.list_for(account_id=account_id, entity_type=entity_type,
                                   entity_id=eid, limit=500)
        out = []
        for row in rows:
            ctype, cid = counterpart(
                {"from_type": row.from_type, "from_id": row.from_id,
                 "to_type": row.to_type, "to_id": row.to_id},
                entity_type, eid,
            )
            ctype = getattr(ctype, "value", ctype)
            if ctype == want_type:
                out.append(cid)
        return out

    async def delete_all_for(self, *, account_id: Any, entity_type: str,
                             entity_id: Any) -> int:
        """Drop every association for a record being deleted, decrementing
        the counterparties' counters. Never cascades to the records themselves.
        """
        acct = self.oid(account_id)
        eid = self.oid(entity_id)
        if not acct or not eid or self.collection is None:
            return 0
        rows = await self.list_for(account_id=acct, entity_type=entity_type,
                                   entity_id=eid, limit=1000)
        for row in rows:
            await self._bump(acct, getattr(row.from_type, "value", row.from_type),
                             row.from_id, getattr(row.to_type, "value", row.to_type),
                             row.to_id, delta=-1)
        try:
            res = await self.collection.delete_many(
                qb.associations_of(account_id=acct, entity_type=entity_type, entity_id=eid)
            )
            return int(getattr(res, "deleted_count", 0) or 0)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return 0

    async def set_primary_company(self, *, account_id: Any, deal_id: Any,
                                  company_id: Any, company_name: str = None) -> bool:
        """Point a deal's denormalised company fields at a new primary."""
        acct, did, cid = self.oid(account_id), self.oid(deal_id), self.oid(company_id)
        if not acct or not did or self.db_session is None:
            return False
        try:
            await self.db_session["deals"].update_one(
                {"_id": did, "created_by": acct},
                {"$set": {"primary_company_id": cid,
                          "primary_company_name": company_name}},
            )
            return True
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False

    # ── counters ─────────────────────────────────────────────────────────────
    async def _bump(self, account_id: Any, from_type: str, from_id: Any,
                    to_type: str, to_id: Any, *, delta: int) -> None:
        """Adjust both sides' rollup counters by ``delta``."""
        for owner_type, owner_id, other_type in (
            (from_type, from_id, to_type),
            (to_type, to_id, from_type),
        ):
            field = _COUNTER_FIELD.get((owner_type, other_type))
            coll = _COLLECTION.get(owner_type)
            if not field or not coll or self.db_session is None:
                continue
            try:
                await self.db_session[coll].update_one(
                    {"_id": owner_id, "created_by": account_id},
                    {"$inc": {field: delta}},
                )
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)
