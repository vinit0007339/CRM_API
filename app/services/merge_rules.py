"""Company merge and unmerge. CRM-239 / CRM-240.

MERGE IS THE MOST DESTRUCTIVE OPERATION IN THIS MODULE. It rewrites one record,
re-points everything attached to another, and takes the second out of every
list. There is no transaction available here — Mongo standalone, and the writes
span five collections — so the design has to survive being interrupted halfway.

The order is therefore fixed, and it is not the obvious one:

1. **Write the snapshot first.** Before anything moves. A snapshot written last
   would be missing precisely when it is needed: after a crash mid-merge.
2. Move the attached records, recording *which ids moved*.
3. Apply the field resolution to the survivor.
4. Soft-delete the loser last.

Interrupted after (2), the result is a survivor holding both sets of records and
a loser still visible — untidy, and completely reversible, because the snapshot
already knows what moved.

⚠️ **UNMERGE MOVES BACK ONLY WHAT THE MERGE MOVED.** This is the whole
difficulty of the feature, and the reason the snapshot records ids rather than
counts. A week after a merge the survivor has notes, tasks and deals of its own.
"Move everything back that points at the survivor" would hand the loser records
it never had — silently, and in a direction nobody would think to check. So each
moved id is written down, and unmerge is the exact inverse of that list.

THE CALLER CHOOSES A SIDE PER FIELD, NOT A VALUE. ``field_resolution`` maps a
field to ``"survivor"`` or ``"merged"``, which is the contract Phase 1 froze and
the better one: a merge endpoint that accepted arbitrary values would be a way
to write data the user never saw on the comparison screen, through an operation
nobody audits afterwards. It also matches what CRM-241 describes — a
side-by-side picker where you click left or right.

Beyond an explicit side, the server fills blanks and nothing else. Guessing
which of two values is "better" — newer, longer, non-empty — looks helpful and
is how a merge quietly discards the correct phone number.

Stdlib only. No DB, no pydantic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# How long a merge stays reversible. After this the sweep drops the snapshot
# and hard-deletes the loser.
UNMERGE_WINDOW_DAYS = 30

# Fields a merge may resolve. Deliberately explicit: `created_by` is tenancy,
# `id` is identity, and the counters are derived — letting a caller "resolve"
# any of them would be a way to move a record between accounts.
MERGEABLE_FIELDS = (
    "name", "domain", "website", "linkedin_url", "industry", "employee_count",
    "annual_revenue", "currency", "phone", "phone_country_code", "description",
    "owner_id", "address", "tag_ids", "custom_fields",
)

# Never resolvable, and named so the rejection can say why.
PROTECTED_FIELDS = frozenset({
    "id", "_id", "created_by", "created_at", "created_by_user_id",
    "is_archived", "merged_into_id", "name_normalised",
    "contact_count", "deal_count", "open_deal_value", "won_deal_value",
    "open_task_count", "overdue_task_count", "next_activity_at",
})

# The collections a merge re-points, and the field on each that points at a
# company. Every one of them is moved and every one is recorded — CRM-239's AC
# asks for a test that creates one of each, and this tuple is what that test
# iterates.
ATTACHED_COLLECTIONS = (
    ("associations", "crm_associations"),
    ("activities", "crm_activities"),
    ("notes", "crm_notes"),
    ("tasks", "crm_tasks"),
    ("files", "crm_files"),
    ("deals", "deals"),
)


# The two sides a field can be resolved to. Frozen in Phase 1's
# CompanyMergeRequest; see the module docstring for why picking a side beats
# accepting a value.
SURVIVOR, MERGED = "survivor", "merged"


class MergeError(Exception):
    """A merge that must not proceed. The message is shown to the user."""


def validate(survivor: Any, loser: Any, resolution: Optional[Dict[str, Any]]) -> None:
    """Refuse the merges that would destroy data. Raises ``MergeError``."""
    if survivor is None or loser is None:
        raise MergeError("One of those companies no longer exists.")

    survivor_id = _get(survivor, "id")
    loser_id = _get(loser, "id")
    if str(survivor_id) == str(loser_id):
        # Not a no-op: the loser is soft-deleted at the end, so merging a
        # record into itself would archive it and move its records onto
        # itself. It reads like a harmless mistake and is a deletion.
        raise MergeError("A company can't be merged into itself.")

    if _get(survivor, "is_archived") or _get(loser, "is_archived"):
        raise MergeError("Archived companies can't be merged. Restore it first.")

    if _get(loser, "merged_into_id"):
        raise MergeError("That company has already been merged into another one.")

    for field, side in (resolution or {}).items():
        if field in PROTECTED_FIELDS:
            raise MergeError(f"{field} can't be chosen during a merge.")
        if field not in MERGEABLE_FIELDS:
            raise MergeError(f"{field} isn't a field that can be merged.")
        if side not in (SURVIVOR, MERGED):
            raise MergeError(
                f'Choose "{SURVIVOR}" or "{MERGED}" for {field}, not "{side}".')


def resolve_fields(survivor: Any, loser: Any,
                   resolution: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """The changes to apply to the survivor.

    Three rules, in order:

    1. an explicit side from the caller wins;
    2. otherwise the survivor keeps its value;
    3. **unless the survivor's value is blank**, in which case the loser's
       fills it — the one case where taking the other record's value cannot
       lose information.

    Nothing here compares values to decide which is "better". A merge that
    prefers the longer description, or the more recent phone number, is
    guessing, and the times it guesses wrong are invisible.
    """
    changes: Dict[str, Any] = {}
    choices = resolution or {}

    for field in MERGEABLE_FIELDS:
        side = choices.get(field)
        if side == MERGED:
            changes[field] = _get(loser, field)
            continue
        if side == SURVIVOR:
            # Recorded explicitly rather than skipped: "keep mine" must beat
            # the blank-filling rule below, or choosing to clear a field would
            # silently re-fill it from the record being merged away.
            changes[field] = _get(survivor, field)
            continue
        mine = _get(survivor, field)
        theirs = _get(loser, field)
        if _is_blank(mine) and not _is_blank(theirs):
            changes[field] = theirs

    # Unions rather than replacements: a tag or a custom field present on only
    # one of the two is information, and dropping it is the merge losing data
    # nobody chose to lose.
    # Unioned rather than resolved to a side, UNLESS a side was named. A tag or
    # a custom field present on only one of the two is information, and
    # dropping it is the merge losing data nobody chose to lose.
    if choices.get("tag_ids") == SURVIVOR:
        changes["tag_ids"] = list(_get(survivor, "tag_ids") or [])
    elif choices.get("tag_ids") == MERGED:
        changes["tag_ids"] = list(_get(loser, "tag_ids") or [])
    else:
        changes["tag_ids"] = _union_ids(_get(survivor, "tag_ids"),
                                        _get(loser, "tag_ids"))

    if choices.get("custom_fields") == SURVIVOR:
        changes["custom_fields"] = dict(_get(survivor, "custom_fields") or {})
    elif choices.get("custom_fields") == MERGED:
        changes["custom_fields"] = dict(_get(loser, "custom_fields") or {})
    else:
        merged_custom = dict(_get(loser, "custom_fields") or {})
        merged_custom.update(
            {k: v for k, v in (_get(survivor, "custom_fields") or {}).items()
             if not _is_blank(v)})
        changes["custom_fields"] = merged_custom

    return changes


def snapshot(survivor: Any, loser: Any) -> Dict[str, Any]:
    """Everything unmerge needs, taken BEFORE anything changes.

    Both records, not just the loser: the field resolution overwrites values on
    the survivor too, and an unmerge that restored only the loser would leave
    the survivor holding the loser's phone number for ever.
    """
    return {
        "survivor": _fields_of(survivor),
        "loser": _fields_of(loser),
        "moved": {name: [] for name, _collection in ATTACHED_COLLECTIONS},
    }


def _fields_of(record: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field in MERGEABLE_FIELDS:
        out[field] = _get(record, field)
    return out


def restore_changes(snapshot_fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The changes that put a record back the way the snapshot found it."""
    return dict(snapshot_fields or {})


def expiry(when: datetime, days: int = UNMERGE_WINDOW_DAYS) -> datetime:
    return _as_utc(when) + timedelta(days=days)


def is_reversible(merged_at: Any, now: Optional[datetime] = None,
                  days: int = UNMERGE_WINDOW_DAYS) -> bool:
    """Is this merge still inside its window?

    A merge with no recorded date is treated as NOT reversible rather than as
    reversible-for-ever: the snapshot it would need is the thing that is
    missing.
    """
    if merged_at is None:
        return False
    now = _as_utc(now or datetime.now(timezone.utc))
    return now < expiry(_as_utc(merged_at), days)


def days_left(merged_at: Any, now: Optional[datetime] = None,
              days: int = UNMERGE_WINDOW_DAYS) -> int:
    if merged_at is None:
        return 0
    now = _as_utc(now or datetime.now(timezone.utc))
    remaining = (expiry(_as_utc(merged_at), days) - now).total_seconds()
    return max(0, int(remaining // 86400))


def summarise(loser_name: str, moved: Optional[Dict[str, List[Any]]]) -> str:
    """What the activity entry says. Counts, so the timeline is specific.

    "Merged a company" is not something anyone can act on a month later;
    "Merged Acme Ltd — 3 deals, 2 notes moved" is.
    """
    counts = [(name, len(ids or [])) for name, ids in (moved or {}).items()]
    parts = [f"{count} {name}" for name, count in counts if count]
    if not parts:
        return f"Merged {loser_name}"
    return f"Merged {loser_name} — {', '.join(parts)} moved"


# ── helpers ──────────────────────────────────────────────────────────────────
def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    # 0 and 0.0 are real values — an employee count of zero is a statement, and
    # treating it as blank would let the merge overwrite it.
    return False


def _union_ids(first: Any, second: Any) -> List[Any]:
    """Union, order-stable, de-duplicated by string form."""
    out: List[Any] = []
    seen: Set[str] = set()
    for value in [*(first or []), *(second or [])]:
        key = str(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _as_utc(value: datetime) -> datetime:
    return value if getattr(value, "tzinfo", None) else value.replace(
        tzinfo=timezone.utc)


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)
