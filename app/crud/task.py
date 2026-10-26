"""Task CRUD. CRM-222 / CRM-223 / CRM-224.

Two responsibilities beyond the base class:

* one filter builder shared by the list, the count and the bucket tallies, so
  the group headings cannot disagree with the rows under them;
* the ``$inc`` counter updates on deals and companies (CRM-224), issued
  directly against the collections rather than through their CRUD classes,
  because loading a deal to change two display integers is a read we do not
  need.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config import logger as _logger
from app.models.company import CompanyModel
from app.models.deal import DealModel
from app.models.task import CrmTaskModel

from app.services import task_rules as tr
from app.services.queries import assert_account_scoped

from .base import CrmCrudBase

# Which collection holds the counters for each link kind. Taken from the models
# rather than written out, because a literal here that drifts from
# ``Settings.name`` fails silently: update_many on a collection nobody uses
# reports success and modifies nothing.
_COUNTER_COLLECTIONS = {
    "deal": DealModel.Settings.name,
    "company": CompanyModel.Settings.name,
}


class CrmTaskCrud(CrmCrudBase):
    model = CrmTaskModel

    # ── reads ────────────────────────────────────────────────────────────────
    def build_filter(self, *, account_id: Any, **params) -> Dict[str, Any]:
        """Delegates to ``task_rules.list_filter`` after normalising ids.

        The route never builds a filter itself; it passes its query params
        straight through. That is the only way the list, the count and the
        bucket tallies stay the same query.
        """
        for key in ("assigned_to_id", "deal_id", "company_id", "contact_id"):
            if params.get(key) is not None:
                params[key] = self.oid(params[key])
        return assert_account_scoped(
            tr.list_filter(account_id=self.oid(account_id), **params))

    async def list_tasks(self, filters: Dict[str, Any], *, sort=None,
                         skip: int = 0, limit: int = 50) -> List[CrmTaskModel]:
        return await self.list_by(filters, sort=sort or tr.sort_spec(),
                                  skip=skip, limit=limit)

    async def count_tasks(self, filters: Dict[str, Any]) -> int:
        return await self.count_by(filters)

    async def bucket_counts(self, *, account_id: Any, assigned_to_id: Any = None,
                            now=None, tz_name: Optional[str] = None
                            ) -> Dict[str, int]:
        """How many open tasks sit in each due bucket.

        Built from the SAME filter the list uses, one count per bucket — so the
        number on a group heading is the number of rows the list would return
        for that group, by construction rather than by hope.
        """
        out: Dict[str, int] = {}
        for bucket in ("overdue", "today", "tomorrow", "week", "later",
                       "no_due_date"):
            flt = self.build_filter(
                account_id=account_id, status="todo", due=bucket,
                assigned_to_id=assigned_to_id, now=now, tz_name=tz_name)
            out[bucket] = await self.count_by(flt)
        return out

    async def for_record(self, *, account_id: Any, entity_type: str,
                         entity_id: Any, open_only: bool = False,
                         limit: int = 200) -> List[CrmTaskModel]:
        """Every task on one record — the detail-screen widget, and the sweep
        that runs when the record itself is deleted."""
        flt = self.build_filter(
            account_id=account_id, status="todo" if open_only else None,
            **{f"{entity_type}_id": entity_id})
        return await self.list_by(flt, sort=tr.sort_spec(), limit=limit)

    # ── counters (CRM-224) ───────────────────────────────────────────────────
    async def apply_counter_deltas(self, *, account_id: Any,
                                   links: List[Tuple[str, Any]],
                                   deltas: Dict[str, int]) -> None:
        """``$inc`` the task counters on every linked deal and company.

        Display-only, per the Phase 1 rule for counters: nothing reads them to
        decide anything, so drift shows a wrong number and never causes wrong
        behaviour. They *will* drift — a process killed between the task write
        and this call leaves them off by one — which is what
        ``scripts/reconcile_crm_task_counters.py`` is for.

        Contacts are omitted deliberately: the contact model is not ours and
        adding CRM counters to it would be a schema change to a collection the
        rest of the product owns.
        """
        if not any(deltas.values()):
            return
        acct = self.oid(account_id)
        if not acct or self.db_session is None:
            return

        grouped: Dict[str, List[Any]] = {}
        for kind, record_id in links:
            name = _COUNTER_COLLECTIONS.get(kind)
            rid = self.oid(record_id)
            if name and rid:
                grouped.setdefault(name, []).append(rid)

        for name, ids in grouped.items():
            try:
                await self.db_session[name].update_many(
                    {"_id": {"$in": ids}, "created_by": acct},
                    {"$inc": {k: v for k, v in deltas.items() if v}},
                )
            except Exception as ex:  # pylint: disable=broad-except
                # A counter must never fail the operation that triggered it.
                _logger.exception(ex)

    async def refresh_next_activity(self, *, account_id: Any,
                                    links: List[Tuple[str, Any]]) -> None:
        """Recompute ``next_activity_at`` — the soonest open task's due date.

        This one cannot be a delta: removing the soonest task means the new
        value is the *second* soonest, which no increment can know. So it is a
        query per affected record, which is why it is called only when the due
        date or the done state actually changed.
        """
        acct = self.oid(account_id)
        if not acct or self.db_session is None:
            return
        for kind, record_id in links:
            name = _COUNTER_COLLECTIONS.get(kind)
            rid = self.oid(record_id)
            if not name or not rid:
                continue
            flt = self.build_filter(account_id=account_id, status="todo",
                                    **{f"{kind}_id": rid})
            flt["has_due_date"] = True
            try:
                soonest = await self.model.find(flt).sort(
                    [("due_at", tr.ASCENDING)]).limit(1).to_list()
                await self.db_session[name].update_one(
                    {"_id": rid, "created_by": acct},
                    {"$set": {"next_activity_at":
                              soonest[0].due_at if soonest else None}},
                )
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)

    # ── the reminder sweep (CRM-227) ─────────────────────────────────────────
    async def due_reminders(self, *, now, grace_minutes: int = 60,
                            limit: int = 200) -> List[CrmTaskModel]:
        """Unclaimed reminders whose fire time has passed.

        NOT account-scoped — this is the only query in the module that isn't,
        because the scheduler runs for every account at once. It therefore does
        not go through ``list_by``/``assert_account_scoped``; the exception is
        deliberate and confined to this one method.

        The window is bounded at both ends. Without the lower bound, a task
        whose reminder was never sent (because the scheduler was down for a
        week) is re-examined on every sweep forever.
        """
        now = tr.as_utc(now)
        # The widest possible fire time is due_at itself (minutes_before = 0);
        # the earliest we still care about is the grace boundary. Reminders can
        # be set well before the due date, so the due_at window is opened by
        # the largest offset we allow.
        earliest = now - timedelta(minutes=grace_minutes)
        latest = now + timedelta(minutes=tr.MAX_REMINDER_MINUTES)
        try:
            rows = await self.model.find({
                "reminder.sent_at": None,
                "is_done": False,
                "has_due_date": True,
                "due_at": {"$type": "date", "$gte": earliest, "$lte": latest},
            }).sort([("due_at", tr.ASCENDING)]).limit(limit).to_list()
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return []
        return [r for r in rows
                if tr.reminder_is_due(r.model_dump(), now,
                                      grace_minutes=grace_minutes)]

    async def claim_reminder(self, task_id: Any, now) -> bool:
        """Take the reminder, or report that someone else already has.

        ONE conditional update. See ``task_rules.claim_filter`` for why this is
        the only thing making the scheduler safe under more than one worker.
        Returns True only if THIS call was the one that changed the document.
        """
        if self.collection is None:
            return False
        try:
            result = await self.collection.update_one(
                tr.claim_filter(self.oid(task_id), now), tr.claim_update(now))
            return bool(getattr(result, "modified_count", 0))
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False
