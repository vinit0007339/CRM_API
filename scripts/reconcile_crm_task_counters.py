"""Recompute the task counters on deals and companies. CRM-224.

``open_task_count``, ``overdue_task_count`` and ``next_activity_at`` are
maintained by ``$inc`` from the task routes. That is fast and it is wrong
eventually: a process killed between the task write and the counter write
leaves the record off by one, and nothing ever notices, because nothing reads
these fields to decide anything. They are display-only by design — drift shows
a wrong badge, never wrong behaviour.

This script is the other half of that bargain. Run it nightly.

    python3 scripts/reconcile_crm_task_counters.py --dry-run
    python3 scripts/reconcile_crm_task_counters.py --account <id>

``--dry-run`` reports the drift without writing, which is also the honest way
to find out whether a counter bug exists: if a nightly dry run keeps reporting
the same records, the fix belongs in the write path, not here.

It is one query per record, which is an N+1 — deliberate, for a job that runs
once a night against a collection measured in thousands. If deals ever reach
the point where this matters, the replacement is a single ``$group`` over
``crm_tasks`` rather than a faster loop.

The recomputation counts from ``crm_tasks`` itself, which is the source of
truth. "Overdue" is evaluated in the ACCOUNT's timezone, exactly as the API
does — reconciling against a UTC "today" would introduce the very drift it is
meant to remove, twice a day, for every account.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, ".")

# ⚠️ THE DB LIFECYCLE IS THE SERVICE'S NOW. In the monolith these
# scripts called `app.init_app_db` / `close_app_db`, which also
# registered every platform model. Here it is `app.db.init_db`, which
# registers the 14 CRM documents and nothing else — a script that
# cannot touch the platform's collections cannot migrate them by
# accident.
from app.db import close_db, init_db                         # noqa: E402
from app.models.company import CompanyModel           # noqa: E402
from app.models.deal import DealModel                 # noqa: E402
from app.models.settings import CrmSettingsModel      # noqa: E402
from app.models.task import CrmTaskModel              # noqa: E402
from app.object_id import PyObjectId            # noqa: E402

from app.services import task_rules as tr              # noqa: E402

LINKS = (("deal", DealModel, "deal_ids"), ("company", CompanyModel, "company_ids"))


async def _timezones() -> Dict[str, str]:
    """account_id → zone name, read once rather than per record."""
    out: Dict[str, str] = {}
    async for row in CrmSettingsModel.find({}):
        out[str(row.created_by)] = getattr(row, "timezone", None) or tr.DEFAULT_TIMEZONE
    return out


async def _expected(model, field: str, account_id: Any, record_id: Any,
                    now: datetime, tz_name: Optional[str]
                    ) -> Tuple[int, int, Optional[datetime]]:
    """Count from crm_tasks — the source of truth — not from the record."""
    open_count = 0
    overdue = 0
    soonest: Optional[datetime] = None

    async for task in CrmTaskModel.find({"created_by": account_id,
                                         field: record_id, "is_done": False}):
        open_count += 1
        if tr.is_overdue(task.due_at, now):
            overdue += 1
        due = tr.as_utc(task.due_at)
        if due is not None and (soonest is None or due < soonest):
            soonest = due
    return open_count, overdue, soonest


def _differs(a: Optional[datetime], b: Optional[datetime]) -> bool:
    """Compare instants, not representations.

    Stored values are naive (PyMongo strips tzinfo) while the computed one is
    aware. A plain ``!=`` would report every record as drifted, every night,
    for ever — and the report would be ignored within a week.
    """
    return tr.as_utc(a) != tr.as_utc(b)


async def reconcile(*, account: Optional[str] = None, dry_run: bool = False,
                    verbose: bool = False) -> Dict[str, int]:
    now = datetime.now(timezone.utc)
    zones = await _timezones()
    stats = {"checked": 0, "drifted": 0, "fixed": 0}

    for kind, model, field in LINKS:
        query: Dict[str, Any] = {}
        if account:
            query["created_by"] = PyObjectId(account)

        async for record in model.find(query):
            stats["checked"] += 1
            tz_name = zones.get(str(record.created_by))
            want_open, want_overdue, want_next = await _expected(
                model, field, record.created_by, record.id, now, tz_name)

            drifted = (
                (record.open_task_count or 0) != want_open
                or (record.overdue_task_count or 0) != want_overdue
                or _differs(record.next_activity_at, want_next)
            )
            if not drifted:
                continue

            stats["drifted"] += 1
            if verbose or dry_run:
                print(f"  {kind} {record.id} "
                      f"open {record.open_task_count}→{want_open}  "
                      f"overdue {record.overdue_task_count}→{want_overdue}  "
                      f"next {record.next_activity_at}→{want_next}")
            if dry_run:
                continue

            record.open_task_count = want_open
            record.overdue_task_count = want_overdue
            record.next_activity_at = want_next
            await record.save()
            stats["fixed"] += 1

    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="reconcile one account only")
    parser.add_argument("--dry-run", action="store_true",
                        help="report drift without writing")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    await init_db()
    try:
        stats = await reconcile(account=args.account, dry_run=args.dry_run,
                                verbose=args.verbose)
    finally:
        await close_db()
    verb = "would fix" if args.dry_run else "fixed"
    print(f"checked {stats['checked']} records · "
          f"{stats['drifted']} drifted · {verb} {stats['fixed']}")
    # A non-zero exit on a dry run that found drift, so this can be a cron
    # check rather than something someone has to read the output of.
    return 1 if (args.dry_run and stats["drifted"]) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
