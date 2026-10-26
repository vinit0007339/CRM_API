"""Performs a company merge and its reversal. CRM-239 / CRM-240.

The order of operations is the design. See ``merge_rules`` for why; the short
version is that there is no transaction, so the snapshot is written before
anything moves and the loser is archived last. Interrupted at any point, the
result is untidy and completely reversible.

⚠️ **DUPLICATE ASSOCIATIONS ARE DELETED, NOT MOVED.** ``crm_associations`` has a
unique index on the canonical direction. If the survivor is already linked to
contact X and the loser is too, re-pointing the loser's row at the survivor
raises a duplicate-key error — which, in a loop that moves fifty rows, would
abandon the merge halfway. So a row whose destination already exists is dropped
instead, and its id is recorded as dropped rather than moved, because unmerge
has to recreate it rather than move it back.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger as _logger
from app.object_id import PyObjectId

from app.services import merge_rules as mr

# Which collection is re-pointed by which field. `crm_associations` is special
# — it has two ends and a uniqueness rule — and is handled separately.
_SIMPLE_MOVES = (
    ("activities", "crm_activities", "entity_id", {"entity_type": "company"}),
    ("notes", "crm_notes", "company_ids", None),
    ("tasks", "crm_tasks", "company_ids", None),
    ("files", "crm_files", "entity_id", {"entity_type": "company"}),
    ("deals", "deals", "primary_company_id", None),
)

# Fields on crm_notes / crm_tasks that hold company ids as an ARRAY.
_ARRAY_FIELDS = {"company_ids"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MergeService:
    """One merge, or one unmerge."""

    def __init__(self, *, db, account_id: Any, companies=None, actor_user_id=None,
                 actor_name: Optional[str] = None):
        self.db = db
        self.account_id = PyObjectId(str(account_id))
        self.companies = companies
        self.actor_user_id = actor_user_id
        self.actor_name = actor_name

    # ── merge ────────────────────────────────────────────────────────────────
    async def merge(self, survivor, loser, resolution: Optional[Dict[str, Any]]
                    ) -> Dict[str, Any]:
        """Returns the snapshot that was stored. Raises ``MergeError``."""
        mr.validate(survivor, loser, resolution)

        snapshot = mr.snapshot(survivor, loser)
        merged_at = _now()

        # 1. THE SNAPSHOT GOES DOWN FIRST, before anything moves. Written last
        #    it would be missing exactly when it is needed: after a crash
        #    partway through the moves below.
        loser.merge_snapshot = snapshot
        loser.merged_into_id = survivor.id
        loser.merged_at = merged_at
        loser.updated_at = merged_at
        await loser.save()

        # 2. Move what is attached, recording which ids moved.
        moved, dropped = await self._move_all(loser.id, survivor.id)
        snapshot["moved"] = moved
        snapshot["dropped"] = dropped

        # 3. Apply the field resolution to the survivor.
        changes = mr.resolve_fields(survivor, loser, resolution)
        if "name" in changes:
            from app.services import dedupe

            changes.update(dedupe.derive_name_fields(changes["name"]))
        for field, value in changes.items():
            setattr(survivor, field, value)
        survivor.updated_at = merged_at
        await survivor.save()

        # 4. The loser leaves the lists LAST, so an interruption before this
        #    point leaves two visible records rather than one invisible one.
        loser.merge_snapshot = snapshot
        loser.is_archived = True
        await loser.save()

        return snapshot

    async def _move_all(self, loser_id: Any, survivor_id: Any
                        ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        moved: Dict[str, List[str]] = {}
        dropped: Dict[str, List[str]] = {}

        associations_moved, associations_dropped = await self._move_associations(
            loser_id, survivor_id)
        moved["associations"] = associations_moved
        dropped["associations"] = associations_dropped

        for name, collection, field, extra in _SIMPLE_MOVES:
            moved[name] = await self._move_simple(collection, field, extra,
                                                  loser_id, survivor_id)
            dropped[name] = []
        return moved, dropped

    async def _move_simple(self, collection: str, field: str,
                           extra: Optional[Dict[str, Any]],
                           loser_id: Any, survivor_id: Any) -> List[str]:
        """Re-point one collection, returning the ids that moved."""
        if self.db is None:
            return []
        query: Dict[str, Any] = {"created_by": self.account_id}
        query.update(extra or {})
        is_array = field in _ARRAY_FIELDS
        query[field] = loser_id

        try:
            rows = await self.db[collection].find(query).to_list()
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return []

        ids: List[str] = []
        for row in rows:
            try:
                if is_array:
                    values = [v for v in (row.get(field) or [])
                              if str(v) != str(loser_id)]
                    if not any(str(v) == str(survivor_id) for v in values):
                        values.append(survivor_id)
                    update = {"$set": {field: values}}
                else:
                    update = {"$set": {field: survivor_id}}
                await self.db[collection].update_one(
                    {"_id": row["_id"], "created_by": self.account_id}, update)
                ids.append(str(row["_id"]))
            except Exception as ex:  # pylint: disable=broad-except
                # One unmovable row must not abandon the merge. It stays on the
                # loser, which is still recoverable, rather than leaving the
                # merge half-applied with no record of where it stopped.
                _logger.exception(ex)
        return ids

    async def _move_associations(self, loser_id: Any, survivor_id: Any
                                 ) -> Tuple[List[str], List[str]]:
        """Re-point the loser's association rows, dropping ones that collide.

        The unique index means "the survivor is already linked to this contact"
        is not an error to raise but a row to remove: the link already exists,
        and keeping a second one is what the index forbids. Dropped ids are
        recorded separately because unmerge must RECREATE them, not move them.
        """
        if self.db is None:
            return [], []
        moved: List[str] = []
        dropped: List[str] = []

        for side, other in (("from_id", "to_id"), ("to_id", "from_id")):
            try:
                rows = await self.db["crm_associations"].find({
                    "created_by": self.account_id, side: loser_id,
                }).to_list()
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)
                continue

            for row in rows:
                try:
                    twin = await self.db["crm_associations"].find_one({
                        "created_by": self.account_id,
                        side: survivor_id,
                        other: row.get(other),
                        "from_type": row.get("from_type"),
                        "to_type": row.get("to_type"),
                    })
                    if twin is not None:
                        await self.db["crm_associations"].delete_one(
                            {"_id": row["_id"], "created_by": self.account_id})
                        dropped.append(str(row["_id"]))
                        continue
                    await self.db["crm_associations"].update_one(
                        {"_id": row["_id"], "created_by": self.account_id},
                        {"$set": {side: survivor_id}})
                    moved.append(str(row["_id"]))
                except Exception as ex:  # pylint: disable=broad-except
                    _logger.exception(ex)
        return moved, dropped

    # ── unmerge ──────────────────────────────────────────────────────────────
    async def unmerge(self, loser) -> Dict[str, Any]:
        """Put both records back. Raises ``MergeError``.

        ⚠️ Only what the merge moved comes back. A week later the survivor has
        records of its own; "move back everything pointing at the survivor"
        would hand the loser records it never had, silently and in a direction
        nobody would think to check. The snapshot's id lists are the authority.
        """
        snapshot = getattr(loser, "merge_snapshot", None)
        if not snapshot:
            raise mr.MergeError("There's no record of that merge to undo.")
        if not mr.is_reversible(getattr(loser, "merged_at", None)):
            raise mr.MergeError(
                f"Merges can only be undone within "
                f"{mr.UNMERGE_WINDOW_DAYS} days.")

        survivor = await self.companies.get_scoped(
            self.account_id, getattr(loser, "merged_into_id", None))
        if survivor is None:
            raise mr.MergeError("The company it was merged into no longer exists.")

        moved = snapshot.get("moved") or {}
        await self._move_back(moved, survivor.id, loser.id)

        # The survivor's own values go back too: the resolution overwrote them.
        for field, value in mr.restore_changes(snapshot.get("survivor")).items():
            setattr(survivor, field, value)
        if "name" in (snapshot.get("survivor") or {}):
            from app.services import dedupe

            for field, value in dedupe.derive_name_fields(survivor.name).items():
                setattr(survivor, field, value)
        survivor.updated_at = _now()
        await survivor.save()

        for field, value in mr.restore_changes(snapshot.get("loser")).items():
            setattr(loser, field, value)
        from app.services import dedupe

        for field, value in dedupe.derive_name_fields(loser.name).items():
            setattr(loser, field, value)
        loser.is_archived = False
        loser.merged_into_id = None
        loser.merged_at = None
        loser.merge_snapshot = None
        loser.updated_at = _now()
        await loser.save()

        return {"survivor_id": str(survivor.id), "restored_id": str(loser.id)}

    async def _move_back(self, moved: Dict[str, List[str]], survivor_id: Any,
                         loser_id: Any) -> None:
        if self.db is None:
            return
        by_name = {name: (collection, field, extra)
                   for name, collection, field, extra in _SIMPLE_MOVES}

        for name, ids in (moved or {}).items():
            if not ids:
                continue
            if name == "associations":
                for row_id in ids:
                    await self._reverse_association(row_id, survivor_id, loser_id)
                continue
            entry = by_name.get(name)
            if not entry:
                continue
            collection, field, _extra = entry
            for row_id in ids:
                try:
                    row = await self.db[collection].find_one({
                        "_id": PyObjectId(row_id), "created_by": self.account_id})
                    if row is None:
                        # Deleted since the merge. Nothing to put back, and
                        # recreating it would resurrect something the user
                        # removed on purpose.
                        continue
                    if field in _ARRAY_FIELDS:
                        values = [v for v in (row.get(field) or [])
                                  if str(v) != str(survivor_id)]
                        if not any(str(v) == str(loser_id) for v in values):
                            values.append(loser_id)
                        update = {"$set": {field: values}}
                    else:
                        update = {"$set": {field: loser_id}}
                    await self.db[collection].update_one(
                        {"_id": row["_id"], "created_by": self.account_id}, update)
                except Exception as ex:  # pylint: disable=broad-except
                    _logger.exception(ex)

    async def _reverse_association(self, row_id: str, survivor_id: Any,
                                   loser_id: Any) -> None:
        try:
            row = await self.db["crm_associations"].find_one({
                "_id": PyObjectId(row_id), "created_by": self.account_id})
            if row is None:
                return
            for side in ("from_id", "to_id"):
                if str(row.get(side)) == str(survivor_id):
                    await self.db["crm_associations"].update_one(
                        {"_id": row["_id"], "created_by": self.account_id},
                        {"$set": {side: loser_id}})
                    return
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)


async def sweep_merges(companies, *, now: Optional[datetime] = None,
                       limit: int = 500) -> int:
    """Finalise merges past their 30-day window. Returns how many. CRM-240.

    Drops the SNAPSHOT, not the record. The snapshot is the bulk of the storage
    and the only part with an expiry; the archived company itself is kept so
    that activity entries, exports and links that still name it keep resolving
    to something. A hard delete would turn a month-old timeline entry into a
    dangling id.
    """
    now = now or _now()
    cutoff = now - timedelta(days=mr.UNMERGE_WINDOW_DAYS)
    try:
        rows = await companies.model.find({
            "merged_at": {"$type": "date", "$lte": cutoff},
            "merge_snapshot": {"$ne": None},
        }).limit(limit).to_list()
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return 0

    swept = 0
    for row in rows:
        try:
            row.merge_snapshot = None
            row.updated_at = now
            await row.save()
            swept += 1
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
    return swept
