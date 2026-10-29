"""Executes a bulk action. CRM-204.

Shared by ``POST /crm/companies/bulk`` and ``POST /crm/deals/bulk`` — one
implementation, because the two differ only in which filter builder resolves
the selection and which actions are legal, and two copies of "apply a change to
a set of records with a per-record permission check" is two places for the
counts to be wrong.

⚠️ **A FILTER-BASED SELECTION IS RESOLVED THROUGH THE SAME CRUD CALL THE LIST
ENDPOINT USES.** "Select all 2,847 matching" is a promise about a set nobody
has read; if this resolved filters independently it would eventually act on a
different set than the screen showed, and the direction that goes is "it
changed records I did not mean".

Per-record, not ``update_many``: every record needs its own ownership check and
its own activity entry, and a single statement can give neither. The cap is
what keeps that bounded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.config import logger as _logger
from app.object_id import PyObjectId

from app.services import (
    activity_recorder as ar, bulk_rules as br,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BulkRunner:
    """One bulk action over one entity type."""

    def __init__(self, *, entity_type: str, account_id: Any, crud,
                 activity=None, actor_user_id: Any = None,
                 actor_name: Optional[str] = None, is_account_owner: bool = False,
                 has_manage_others: bool = False):
        self.entity_type = entity_type
        self.account_id = PyObjectId(str(account_id))
        self.crud = crud
        self.activity = activity
        self.actor_user_id = actor_user_id
        self.actor_name = actor_name
        self.is_account_owner = is_account_owner
        self.has_manage_others = has_manage_others

    async def run(self, *, action: str, payload: Dict[str, Any],
                  ids: Optional[Sequence[Any]], filters: Optional[Dict[str, Any]],
                  hard: bool = False,
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Returns the ``BulkActionResult`` fields. Raises ``BulkError``.

        ``extra`` is an advanced-filter fragment the ROUTE compiled from a
        filter group (E22). It never comes from the client as a fragment — see
        ``bulk_rules.BULK_FILTER_KEYS`` for why that distinction matters on a
        destructive action.
        """
        br.validate(action, payload, self.entity_type)
        clean = br.sanitise_filters(self.entity_type, filters)
        if extra:
            # A fragment alone is a selection in its own right: "everything
            # matching this filter" with no other narrowing.
            clean["extra"] = extra
        selection = br.selection_from(ids, clean or None)

        records, matched, cap_notice = await self._select(selection)

        result = {
            "matched": matched, "updated": 0, "skipped": 0, "failed": 0,
            "skipped_no_permission": 0, "skipped_reason": None,
            "cap_notice": cap_notice,
        }
        reasons: List[str] = []
        activities: List[Optional[Dict[str, Any]]] = []

        for record in records:
            if not br.can_touch(record, actor_user_id=self.actor_user_id,
                                is_account_owner=self.is_account_owner,
                                has_manage_others=self.has_manage_others):
                result["skipped_no_permission"] += 1
                continue

            if action == "move_pipeline" and br.deal_is_closed(record):
                # A closed deal has an outcome; moving it to an open stage
                # would silently reopen it and corrupt every won/lost report.
                result["skipped"] += 1
                if SKIP_CLOSED_TEXT not in reasons:
                    reasons.append(SKIP_CLOSED_TEXT)
                continue

            try:
                changed = await self._apply(action, payload, record, hard=hard)
            except Exception as ex:  # pylint: disable=broad-except
                # One bad record must not abandon the other thousand.
                _logger.exception(ex)
                result["failed"] += 1
                continue

            if changed is None:
                result["skipped"] += 1
                continue

            result["updated"] += 1
            activities.append(changed)

        if reasons:
            result["skipped_reason"] = "; ".join(reasons)
        await self._record(activities)
        # Always permanent since archiving was removed — see `_delete`.
        result["message"] = br.summarise(result, action, permanent=True)
        return result

    # ── selection ────────────────────────────────────────────────────────────
    async def _select(self, selection: Dict[str, Any]):
        """``(records, matched, cap_notice)``."""
        if selection["mode"] == "ids":
            ids = selection["ids"]
            limit, notice = br.apply_cap(len(ids))
            records = []
            for record_id in ids[:limit]:
                # get_scoped, not a bare get: ids arriving from a client are
                # not proof of ownership, and this is a write.
                found = await self.crud.get_scoped(self.account_id, record_id)
                if found is not None:
                    records.append(found)
            return records, len(ids), notice

        filters = dict(selection["filters"] or {})
        # THE SAME CALL THE LIST ENDPOINT MAKES. See the module docstring.
        matched = await self.crud.count(account_id=self.account_id, **filters)
        limit, notice = br.apply_cap(matched)
        records = await self.crud.list(account_id=self.account_id, skip=0,
                                       limit=limit, **filters)
        return records, matched, notice

    @staticmethod
    def _label(record: Any) -> str:
        """What to call this record in its activity entry.

        ⚠️ NOT `record.name`. A CRM contact has no `name` column — it has
        `first_name` / `last_name` and a denormalised `full_name` — so every
        bulk action on one raised AttributeError while BUILDING THE ACTIVITY
        TITLE, after the record had already been saved. The write landed, the
        exception was caught as a failure, and the user was told 0 of 1
        updated about a change that had in fact happened.
        """
        for field in ("name", "full_name", "email"):
            value = getattr(record, field, None)
            if value:
                return str(value)
        return "A record"

    # ── one record ───────────────────────────────────────────────────────────
    async def _apply(self, action: str, payload: Dict[str, Any], record: Any,
                     *, hard: bool) -> Optional[Dict[str, Any]]:
        """Apply to one record. Returns the activity to write, or None to skip."""
        if action == "delete":
            ok = await self._delete(record, hard=hard)
            if not ok:
                raise RuntimeError("delete failed")
            return ar.build_activity(
                account_id=self.account_id, entity_type=self.entity_type,
                entity_id=record.id, activity_type="deleted",
                title=f"{self._label(record)} deleted in a bulk action",
                actor_user_id=self.actor_user_id, actor_name=self.actor_name)

        changes = br.changes_for(action, payload, record)
        if changes is None:
            return None

        attribute = changes.pop("__attribute__", None)
        if attribute:
            merged = dict(getattr(record, "custom_fields", None) or {})
            merged.update(attribute)
            # A value that is already what was asked for is a skip, so the
            # count does not include records nothing happened to.
            if merged == (getattr(record, "custom_fields", None) or {}):
                return None
            changes["custom_fields"] = merged

        for field, value in changes.items():
            setattr(record, field, value)
        record.updated_at = _now()
        await record.save()

        # ONE aggregate activity per record, never one per field — a bulk
        # retag of 400 records must not write 1,200 timeline entries.
        return ar.build_activity(
            account_id=self.account_id, entity_type=self.entity_type,
            entity_id=record.id, activity_type="updated",
            title=f"{self._label(record)} updated in a bulk action",
            metadata={"action": action, "fields": sorted(changes)},
            actor_user_id=self.actor_user_id, actor_name=self.actor_name)

    async def _delete(self, record: Any, *, hard: bool) -> bool:
        """Delete, permanently, always.

        ⚠️ ARCHIVING WAS REMOVED ON 29 OCT 2026 and this is where it lived.
        Companies and contacts used to archive unless `hard` was asked for;
        deals never could. The middle ground had become a one-way door — an
        archived record left every list and no action could then remove it —
        so a delete is now a delete for every object.

        ⚠️ `hard` IS VESTIGIAL AND IGNORED. Kept in the signature so existing
        callers and the `?hard=` query param do not break; it no longer
        selects between two behaviours because there is only one. Mirrors the
        single-record endpoints, which are restricted to an account owner or
        `crm:manage_others` for the same reason: nothing is recoverable now.
        """
        # ⚠️ ALWAYS PERMANENT, AND `hard` IS NOW VESTIGIAL. Archiving was
        # removed on 29 Oct 2026; there is no longer a recoverable state for a
        # delete to fall back to, so every object behaves the way deals always
        # have. The parameter is kept so existing callers do not break, and
        # ignored.
        return await self.crud.delete_scoped(self.account_id, record.id)

    async def _record(self, activities: List[Optional[Dict[str, Any]]]) -> None:
        if not self.activity or not activities:
            return
        try:
            await ar.record_bulk(self.activity.write_many, activities)
        except Exception as ex:  # pylint: disable=broad-except
            # The trail must never fail the writes it describes.
            _logger.exception(ex)


SKIP_CLOSED_TEXT = "closed deals were left where they are"
