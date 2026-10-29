"""Bulk action rules for companies and deals. CRM-204.

A BULK ACTION IS THE ONLY PLACE IN THE MODULE WHERE ONE CLICK CHANGES RECORDS
THE USER CANNOT SEE. "Select all 2,847 matching" is a promise about a set
nobody has read, so three things have to be true of it:

* **the set is the one on screen.** The filter-based selection is resolved
  through the SAME filter builder the list endpoint uses. A second
  implementation would eventually act on a different set than the user was
  looking at, and the direction that goes is "it changed records I did not
  mean";
* **the cap is reported, never silently applied.** A request that matches 9,000
  records and updates 1,000 must say so. Silently truncating looks like success
  and leaves 8,000 records in a state the user believes they changed;
* **the counts are separable.** "12 of 40 updated" is only actionable if the
  other 28 are broken down — permission, business rule, or error are three
  different problems with three different fixes.

PARTIAL FAILURE IS NOT AN ERROR. The response is HTTP 200 with
``status: "success"`` and the counts. A bulk operation that half-worked did
half-work, and returning a failure would tell the client to retry the whole
thing.

Stdlib only. No DB, no pydantic.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# The most records one filter-based bulk action may touch. Not a limit on what
# can be selected — the response reports the true match count — but on what one
# request will do, so a mistyped filter cannot rewrite an entire account in one
# call and time out halfway through.
BULK_CAP = 1000

# ⚠️ NO "archive" FOR ANY OBJECT ANY MORE. Archiving was removed on 29 Oct
# 2026 at the product's request: a deleted record is deleted. Deals never had
# it (see below); companies and contacts did, and it had become a one-way door
# — an archived record left every list and there was no action that could then
# remove it.
#
# ⚠️ SO `delete` IS NOW PERMANENT FOR EVERY OBJECT, and is restricted to an
# account owner or `crm:manage_others` — the gate that used to guard `hard`.
# A plain write grant can no longer destroy a record, because the recoverable
# middle ground it used to have is gone.
COMPANY_ACTIONS = ("set_owner", "set_attribute", "add_tags", "remove_tags",
                   "delete")
# ⚠️ NO "archive" FOR DEALS, AND THAT IS NOT AN OVERSIGHT. `DealModel` has no
# `is_archived` field and `queries.deal_filter` has no parameter to hide one,
# so there is nowhere for an archived deal to go: it would stay on the board,
# in the list, in the board totals and in the forecast. An archived deal still
# sitting in the pipeline is a worse bug than no archive at all.
#
# A deal that is over but worth keeping is CLOSED, not archived — `Mark won` /
# `Mark lost`, which is what win-rate reporting reads. `delete` on a deal is
# therefore permanent, matching the single-record button on its detail page.
#
# This used to include "archive", and `BulkRunner._delete` assigned
# `record.is_archived = True` on a model with no such field — so pydantic
# raised `ValueError: "DealModel" object has no field "is_archived"`, every
# selected deal was counted as `failed`, and a user deleting two deals was
# told "0 updated, 2 failed" with nothing deleted. `hard=false` from the UI
# took that path every time, which is to say bulk delete never worked on a
# deal. `ARCHIVE_NEEDS_A_FIELD` in tests/test_engage_crm_bulk_rules.py is the
# guard that stops a fourth object repeating it.
DEAL_ACTIONS = ("set_owner", "set_attribute", "move_pipeline", "add_tags",
                "remove_tags", "delete")

# E25. The same set as companies: a contact has no pipeline to move between.
# Spelled out rather than inherited from companies, so adding a fourth object
# cannot silently acquire whatever companies happen to allow.
CONTACT_ACTIONS = ("set_owner", "set_attribute", "add_tags", "remove_tags",
                   "delete")

ACTIONS_BY_ENTITY = {
    "company": COMPANY_ACTIONS,
    "deal": DEAL_ACTIONS,
    "contact": CONTACT_ACTIONS,
}

# Why a record was skipped. Kept apart because they are three different
# problems: one needs a permission change, one needs the user to pick different
# records, one needs us to fix something.
SKIP_PERMISSION = "no_permission"
SKIP_CLOSED = "closed"
SKIP_UNCHANGED = "unchanged"


class BulkError(Exception):
    """A request that cannot be performed at all. The message is shown."""


def validate(action: str, payload: Optional[Dict[str, Any]],
             entity_type: str = "company") -> None:
    """Refuse a request that would do nothing, or the wrong thing. Raises."""
    allowed = ACTIONS_BY_ENTITY.get(entity_type)
    if allowed is None:
        # An unknown object must not fall back to another object's action set.
        raise BulkError(f"There are no bulk actions for {entity_type}s.")
    if action not in allowed:
        if action == "archive" and entity_type == "deal":
            # Say what to do instead, rather than only what cannot be done.
            raise BulkError(
                "Deals aren't archived — mark them won or lost to take them "
                "out of the pipeline and keep them for reporting, or delete "
                "them to remove them for good.")
        raise BulkError(f'"{action}" isn\'t something you can do in bulk to '
                        f'{entity_type}s.')

    payload = payload or {}

    if action == "set_owner" and not payload.get("owner_id"):
        # Without an owner this would blank the field on every selected record
        # — a plausible client bug, and a destructive one nobody clicked.
        raise BulkError("Choose who to assign these to.")

    if action in ("add_tags", "remove_tags") and not payload.get("tag_ids"):
        raise BulkError("Choose at least one tag.")

    if action == "set_attribute":
        if not payload.get("internal_name"):
            raise BulkError("Choose which field to set.")
        if "value" not in payload:
            # Distinct from a null value: `value: null` CLEARS the field and is
            # a legitimate request; an absent key is a malformed one.
            raise BulkError("Say what to set it to.")

    if action == "move_pipeline":
        # Both, or the deals land in a pipeline with no stage and disappear
        # from every board.
        if not payload.get("pipeline_id") or not payload.get("stage_id"):
            raise BulkError("Choose both a pipeline and a stage to move these to.")


def is_destructive(action: str) -> bool:
    """Actions the UI must confirm with a count before sending."""
    return action in ("delete", "archive")


# The list parameters a bulk `filters` selection may name, per entity. The
# client sends this dict verbatim and `BulkRunner._select` spreads it into
# `crud.count`/`crud.list`, so it reaches `queries.company_filter` as keyword
# arguments.
#
# ⚠️ `extra` IS DELIBERATELY ABSENT, AND MUST STAY ABSENT. It is the merge
# point for a RAW MONGO FRAGMENT (E22). Accepting it here would let a client
# choose the selection's query operators directly: still inside the account —
# `merge_fragment` refuses `created_by` and `assert_account_scoped` runs — but
# a bulk DELETE whose selection is a query the UI never offered and nobody
# reviewed. The advanced filter reaches bulk as `filter_group`, a filter group
# that `filter_rules` compiles on the server, exactly as on the list route.
#
# An unknown key used to be a TypeError and a 500. Naming them is what turns
# that into a message, and what keeps the set reviewable.
BULK_FILTER_KEYS: Dict[str, frozenset] = {
    "company": frozenset({
        "q", "owner_id", "industry", "tag_ids", "domain", "is_archived",
        "created_from", "created_to", "ids",
    }),
    "deal": frozenset({
        "q", "pipeline_id", "stage_id", "status", "owner_id", "company_id",
        "tag_ids", "amount_min", "amount_max", "close_date_from",
        "close_date_to", "has_overdue_task", "ids",
    }),
}


def sanitise_filters(entity_type: str,
                     filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The client's bulk selection filters, with anything unknown REFUSED.

    Refused, not dropped: a bulk action is destructive, and quietly widening
    its selection by ignoring a narrowing filter it did not recognise is the
    worst possible way to be wrong here.
    """
    if not filters:
        return {}
    allowed = BULK_FILTER_KEYS.get(entity_type)
    if allowed is None:
        raise BulkError("That object cannot be selected by a filter.")
    unknown = sorted(k for k in filters if k not in allowed)
    if unknown:
        raise BulkError(
            f"This selection names something that is not a filter: "
            f"{', '.join(unknown)}.")
    return dict(filters)


def selection_from(ids: Optional[Sequence[Any]],
                   filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``resolve_bulk_selection``, re-exported so callers import one module.

    Kept as a thin wrapper rather than reimplemented: the "either/or, never
    both" rule is already written and tested in ``serializers``.
    """
    from app.services.serializers import resolve_bulk_selection

    try:
        return resolve_bulk_selection(ids, filters)
    except ValueError as ex:
        raise BulkError(str(ex)) from ex


def apply_cap(matched: int, cap: Optional[int] = None) -> Tuple[int, Optional[str]]:
    """``(limit, notice)``. The notice is None when nothing was truncated.

    The notice exists because a silent cap is indistinguishable from success:
    a user who selects 9,000 and is told "1,000 updated" with no explanation
    concludes 8,000 failed, and one who is told nothing concludes all 9,000
    were done.
    """
    # Read at CALL time, not bound as a default: a default argument captures
    # the value at import, so the cap could not be changed at runtime — or
    # patched in a test, which is how this was found.
    cap = BULK_CAP if cap is None else cap
    if matched <= cap:
        return matched, None
    return cap, (f"{matched:,} records matched; this run changed the first "
                 f"{cap:,}. Run it again to continue.")


def can_touch(record: Any, *, actor_user_id: Any, is_account_owner: bool,
              has_manage_others: bool) -> bool:
    """The single-record ownership rule, applied per record in a batch.

    Checked per record rather than once for the batch: a selection of forty
    records can span owners, and "you don't have permission" for the whole
    batch because one record belongs to someone else is not a useful answer.
    """
    if is_account_owner or has_manage_others:
        return True
    owner_id = _get(record, "owner_id")
    if owner_id is None:
        # An unowned record is nobody's to protect.
        return True
    return str(owner_id) == str(actor_user_id)


def deal_is_closed(deal: Any) -> bool:
    """Won or lost.

    ⚠️ ``.value`` FIRST. ``DealStatus`` subclasses ``str``, and ``str(...)`` on
    a member returns "DealStatus.WON", not "won" — so a bare ``str().lower()``
    compares "dealstatus.won" against "won" and reports every closed deal as
    open. This codebase has been bitten by that exact conversion twice before
    (CRM-210's object routing, and ``serializers.json_safe``), which is why
    that helper checks Enum before str.
    """
    raw = _get(deal, "status")
    status = getattr(raw, "value", raw)
    return str(status or "").lower() in ("won", "lost")


def changes_for(action: str, payload: Dict[str, Any], record: Any
                ) -> Optional[Dict[str, Any]]:
    """The field changes for one record, or None if this record should skip.

    Returning None rather than an empty dict distinguishes "nothing to do here"
    from "do nothing" — the first is a skip, the second would be an update that
    writes an empty change and counts as success.
    """
    payload = payload or {}

    if action == "set_owner":
        if str(_get(record, "owner_id") or "") == str(payload["owner_id"]):
            return None
        return {"owner_id": payload["owner_id"]}

    if action == "archive":
        return None if _get(record, "is_archived") else {"is_archived": True}

    if action == "add_tags":
        current = list(_get(record, "tag_ids") or [])
        merged = _union_ids(current, payload.get("tag_ids"))
        return None if len(merged) == len(current) else {"tag_ids": merged}

    if action == "remove_tags":
        current = list(_get(record, "tag_ids") or [])
        drop = {str(t) for t in (payload.get("tag_ids") or [])}
        kept = [t for t in current if str(t) not in drop]
        return None if len(kept) == len(current) else {"tag_ids": kept}

    if action == "move_pipeline":
        if str(_get(record, "pipeline_id") or "") == str(payload["pipeline_id"]) \
                and str(_get(record, "stage_id") or "") == str(payload["stage_id"]):
            return None
        return {"pipeline_id": payload["pipeline_id"],
                "stage_id": payload["stage_id"]}

    if action == "set_attribute":
        return {"__attribute__": {payload["internal_name"]: payload.get("value")}}

    return None


def summarise(result: Dict[str, int], action: str,
              permanent: bool = False) -> str:
    """The sentence in the response.

    Names each bucket, because "12 of 40 updated" without the breakdown is not
    something anyone can act on — permission, a business rule and an error are
    three different problems.

    ``permanent`` says the records were removed for good rather than archived.
    "2 updated" after deleting two deals tells the user nothing about whether
    they can get them back, and for deals they cannot.
    """
    verb = "deleted permanently" if (permanent and action == "delete") else (
        "deleted" if action == "delete" else "updated")
    parts = [f"{result.get('updated', 0):,} {verb}"]
    if result.get("skipped_no_permission"):
        parts.append(f"{result['skipped_no_permission']:,} you don't own")
    if result.get("skipped"):
        parts.append(f"{result['skipped']:,} already as requested")
    if result.get("failed"):
        parts.append(f"{result['failed']:,} failed")
    return ", ".join(parts)


def _union_ids(first: Any, second: Any) -> List[Any]:
    out: List[Any] = []
    seen: Set[str] = set()
    for value in [*(first or []), *(second or [])]:
        key = str(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)
