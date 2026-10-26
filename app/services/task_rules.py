"""Task due-date buckets and reminder timing. CRM-223 / CRM-227.

"TODAY" IS THE MOST DANGEROUS WORD IN THIS EPIC.

A task due at 23:00 IST is due *today* to the person who created it. In UTC
that instant is 17:30 the same day — fine — but a task due at 00:30 IST is
19:00 UTC the **previous** day, and a naive UTC "today" filter puts it in
yesterday's overdue list. The user sees a task they haven't missed reported as
missed, which is exactly the kind of wrongness that makes people stop trusting
a task list.

So every bucket boundary is computed in the ACCOUNT's timezone and then
converted to UTC for the query. The account's zone lives in
``crm_settings.timezone`` and defaults to Asia/Kolkata, which is what the rest
of the app already assumes (``formatDateToIST`` hard-codes +05:30).

Stored datetimes are naive UTC — PyMongo strips tzinfo — so everything here
normalises before comparing. That bug already cost a production 500 on the
board; see ``stage_rules._as_utc``.

Stdlib-only. No DB, no pydantic.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

ASCENDING = 1
DESCENDING = -1

# Fields the client may sort by. Anything else falls back to the default rather
# than being handed to Mongo.
SORT_FIELDS = frozenset({
    "due_at", "created_at", "updated_at", "title", "priority", "task_type",
    "completed_at",
})

DEFAULT_TIMEZONE = "Asia/Kolkata"

# The furthest ahead of the due date a reminder may fire — two weeks. It exists
# to bound the scheduler's sweep window: without a ceiling the sweep would have
# to consider every future task on every pass, because any of them could in
# principle have a reminder set years in advance.
MAX_REMINDER_MINUTES = 14 * 24 * 60

# Fallback when the zone name is unknown or tzdata is unavailable. India has no
# DST, so a fixed offset is exactly right for the default and merely
# approximate for anywhere else — which is better than crashing.
_FALLBACK_OFFSET = timedelta(hours=5, minutes=30)

BUCKETS = ("overdue", "today", "tomorrow", "week", "later", "no_due_date")


def resolve_zone(name: Optional[str] = None):
    """A tzinfo for the account. Never raises."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:  # pylint: disable=broad-except
        return timezone(_FALLBACK_OFFSET)


def as_utc(value: Any) -> Optional[datetime]:
    """Coerce to aware UTC. A naive value out of Mongo is UTC by construction."""
    if value is None or not hasattr(value, "tzinfo"):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def local_day(now: datetime, tz_name: Optional[str] = None) -> date:
    """The calendar date it is *for the account* right now."""
    return as_utc(now).astimezone(resolve_zone(tz_name)).date()


def day_bounds(
    day: date, tz_name: Optional[str] = None
) -> Tuple[datetime, datetime]:
    """``[start, end)`` of one local day, in UTC.

    Half-open on purpose: midnight belongs to the day starting, not the day
    ending, and a closed interval would put a task due at exactly 00:00 in two
    buckets at once.
    """
    zone = resolve_zone(tz_name)
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def bucket_range(
    bucket: str, now: datetime, tz_name: Optional[str] = None
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """``(due_from, due_to)`` in UTC for a named bucket, either side open.

    ``overdue`` is everything strictly before *now* — not before midnight.
    A task due at 09:00 that it is now 10:00 is overdue, and telling the user
    otherwise until tomorrow would be useless.
    """
    now = as_utc(now)
    today = local_day(now, tz_name)

    if bucket == "overdue":
        return None, now
    if bucket == "today":
        start, end = day_bounds(today, tz_name)
        # Not before now: an already-missed task belongs in overdue, and
        # showing it in both makes the counts add up to more than the list.
        return max(start, now), end
    if bucket == "tomorrow":
        return day_bounds(today + timedelta(days=1), tz_name)
    if bucket == "week":
        # The next seven days from now, not "this calendar week" — on a Friday
        # a calendar week means two days, which is not what anyone planning
        # their week means.
        _, end = day_bounds(today + timedelta(days=7), tz_name)
        return now, end
    if bucket == "later":
        _, end = day_bounds(today + timedelta(days=7), tz_name)
        return end, None
    return None, None


def list_filter(
    *,
    account_id: Any,
    status: Optional[str] = None,
    assigned_to_id: Any = None,
    task_type: Optional[str] = None,
    priority: Optional[str] = None,
    due: Optional[str] = None,
    due_from: Any = None,
    due_to: Any = None,
    deal_id: Any = None,
    company_id: Any = None,
    contact_id: Any = None,
    search: Optional[str] = None,
    now: Optional[datetime] = None,
    tz_name: Optional[str] = None,
) -> Dict[str, Any]:
    """The ONE filter the list and its count both use. CRM-223.

    It lives here rather than in ``queries.py`` with the other builders because
    the bucket names it understands are defined three functions up. Split them
    and the day ``bucket_of`` changes is the day the list quietly stops
    matching the label on each row.

    ⚠️ **The ``$type`` guard is not decoration.** In BSON ordering ``null``
    sorts *before* every date, so ``{"due_at": {"$lt": now}}`` matches every
    task with no due date at all — the overdue list would fill up with tasks
    nobody has ever scheduled. Any range on ``due_at`` therefore also asserts
    the field is a date.
    """
    now = as_utc(now or datetime.now(timezone.utc))
    flt: Dict[str, Any] = {"created_by": account_id}

    if status == "todo":
        flt["is_done"] = False
    elif status == "done":
        flt["is_done"] = True
    # "all" (or anything else) deliberately omits the key rather than guessing.

    if assigned_to_id is not None:
        flt["assigned_to_id"] = assigned_to_id
    if task_type:
        flt["task_type"] = task_type
    if priority:
        flt["priority"] = priority

    if deal_id is not None:
        flt["deal_ids"] = deal_id
    if company_id is not None:
        flt["company_ids"] = company_id
    if contact_id is not None:
        flt["contact_ids"] = contact_id

    if search:
        flt["title"] = {"$regex": re.escape(str(search).strip()), "$options": "i"}

    if due == "no_due_date":
        flt["due_at"] = None
        return flt

    lo, hi = (None, None)
    if due == "range":
        lo, hi = as_utc(due_from), as_utc(due_to)
    elif due:
        lo, hi = bucket_range(due, now, tz_name)

    if lo is not None or hi is not None:
        # Half-open, matching day_bounds: $gte start, $lt end.
        window: Dict[str, Any] = {"$type": "date"}
        if lo is not None:
            window["$gte"] = lo
        if hi is not None:
            window["$lt"] = hi
        flt["due_at"] = window
        # Redundant with $type — and kept anyway, because it is an EQUALITY
        # predicate. That turns the index into
        # (created_by, is_done, has_due_date, due_at): equality, equality,
        # equality, range, which is the one ordering Mongo can use for both the
        # window and the sort. The $type guard stays as the correctness
        # statement; this is the one the planner reads.
        flt["has_due_date"] = True
    return flt


def due_fields(due_at: Any) -> Dict[str, Any]:
    """``{due_at, has_due_date}`` — the ONLY way either field is written.

    ``has_due_date`` is redundant with ``due_at is not None``, which is exactly
    why it is dangerous: two fields expressing one fact drift the moment
    something sets one without the other. Every write path goes through here,
    and ``scripts/reconcile_crm_task_counters.py`` re-checks it.

    It exists because sorting cannot do without it. In Mongo a missing or null
    ``due_at`` sorts *before* every date, so "soonest first" would open the list
    with every unscheduled task — the least urgent things on the screen. Fixing
    that at query time needs an aggregation with ``$addFields``; a stored
    boolean keeps the list a plain indexed ``find``.
    """
    due = as_utc(due_at)
    return {"due_at": due, "has_due_date": due is not None}


def sort_spec(sort_by: Optional[str] = None, sort_dir: Optional[str] = None
              ) -> List[Tuple[str, int]]:
    """Sort for the task list, with an unknown field falling back rather than
    reaching Mongo.

    Undated tasks go last whichever direction the dates run — see
    ``due_fields``.
    """
    direction = DESCENDING if str(sort_dir or "").lower() == "desc" else ASCENDING
    field = sort_by if sort_by in SORT_FIELDS else "due_at"
    if field == "due_at":
        return [("has_due_date", DESCENDING), ("due_at", direction),
                ("created_at", DESCENDING)]
    return [(field, direction), ("created_at", DESCENDING)]


def bucket_of(
    due_at: Any, now: datetime, tz_name: Optional[str] = None, *, is_done: bool = False
) -> str:
    """Which bucket one task falls in. Grouping in the UI uses this."""
    if is_done:
        return "done"
    due = as_utc(due_at)
    if due is None:
        return "no_due_date"

    now = as_utc(now)
    if due < now:
        return "overdue"

    today = local_day(now, tz_name)
    _, today_end = day_bounds(today, tz_name)
    if due < today_end:
        return "today"

    _, tomorrow_end = day_bounds(today + timedelta(days=1), tz_name)
    if due < tomorrow_end:
        return "tomorrow"

    _, week_end = day_bounds(today + timedelta(days=7), tz_name)
    if due < week_end:
        return "week"
    return "later"


def is_overdue(due_at: Any, now: datetime, *, is_done: bool = False) -> bool:
    """A completed task is never overdue, whatever its due date was.

    Worth stating because the counter query and the UI badge must agree, and
    "overdue" quietly meaning "was overdue when completed" would inflate every
    dashboard permanently.
    """
    if is_done:
        return False
    due = as_utc(due_at)
    return bool(due and due < as_utc(now))


def counter_deltas(before: Optional[Dict], after: Optional[Dict], now: datetime) -> Dict[str, int]:
    """``{open_task_count, overdue_task_count}`` changes for one task edit.

    CRM-224. Returned as deltas rather than absolutes so the parent record can
    be updated with ``$inc`` — recomputing an absolute would need a count query
    per linked record on every task write.

    Counters are display-only and never read by business logic, so drift cannot
    cause incorrect behaviour. They will still drift; that is what the
    reconciliation script is for.
    """
    def state(task):
        if not task:
            return (0, 0)
        done = bool(task.get("is_done"))
        if done:
            return (0, 0)
        return (1, 1 if is_overdue(task.get("due_at"), now) else 0)

    before_open, before_overdue = state(before)
    after_open, after_overdue = state(after)
    return {
        "open_task_count": after_open - before_open,
        "overdue_task_count": after_overdue - before_overdue,
    }


def linked_ids(task: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """``[(entity_type, id)]`` for every record a task is attached to."""
    out: List[Tuple[str, Any]] = []
    for field, kind in (("deal_ids", "deal"), ("company_ids", "company"),
                        ("contact_ids", "contact")):
        for value in (task or {}).get(field) or []:
            out.append((kind, value))
    return out


# ── reminders (CRM-227) ──────────────────────────────────────────────────────
def reminder_fires_at(due_at: Any, minutes_before: int) -> Optional[datetime]:
    """When a reminder should go out, in UTC."""
    due = as_utc(due_at)
    if due is None:
        return None
    minutes = min(MAX_REMINDER_MINUTES, max(0, int(minutes_before or 0)))
    return due - timedelta(minutes=minutes)


def reminder_is_due(task: Dict[str, Any], now: datetime, *, grace_minutes: int = 60) -> bool:
    """Should this task's reminder be sent right now?

    Four conditions, and the last one is the one that matters:

    * the task has a reminder and a due date;
    * it is not already done;
    * the fire time has passed;
    * ``sent_at`` is not set — that field IS the claim. See ``claim_filter``.

    ``grace_minutes`` stops a scheduler that was down for a day from sending a
    burst of reminders for meetings that already happened. A late reminder is
    noise; a reminder for something two days past is worse than noise.
    """
    reminder = (task or {}).get("reminder") or {}
    if not reminder or reminder.get("sent_at"):
        return False
    if task.get("is_done"):
        return False

    fires = reminder_fires_at(task.get("due_at"), reminder.get("minutes_before"))
    if fires is None:
        return False

    now = as_utc(now)
    if fires > now:
        return False
    return fires >= now - timedelta(minutes=grace_minutes)


def claim_filter(task_id: Any, now: datetime) -> Dict[str, Any]:
    """The filter for a conditional update that CLAIMS a reminder.

    ⚠️ R8: every scheduler in this codebase assumes a single process, and seven
    already do. This one must not make it worse.

    The claim is ``{_id: id, reminder.sent_at: None}`` with ``$set`` of
    ``sent_at``. Two workers running the same sweep both issue it; Mongo
    applies single-document updates atomically, so exactly one matches and
    modifies. The loser sees ``modified_count == 0`` and skips.

    That makes double-send impossible **without** a lock, a leader election or
    a queue — and it is the only reason this is safe to run under more than one
    worker. Do not "optimise" it into a read-then-write.
    """
    return {"_id": task_id, "reminder.sent_at": None}


def claim_update(now: datetime) -> Dict[str, Any]:
    return {"$set": {"reminder.sent_at": as_utc(now)}}
