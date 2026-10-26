"""Pure rules for the account's lost-reason picklist. CRM-019.

WHY THIS IS ITS OWN MODULE, and why it never deletes.

``POST /crm/deals/{id}/close`` refuses a lost deal without a ``lost_reason_id``
and validates that id against this list, and a closed deal keeps only the id —
so a reason that disappears takes the label of every deal that referenced it
with it. ``LostReason`` says as much in the model: *"Soft-retired via
``is_active`` rather than deleted, so historical deals keep resolving their
reason label."*

That is the single rule this module exists to enforce. A row the client omits
is RETIRED AND KEPT, never dropped. The client cannot delete a reason even by
sending a list without it, which means the destructive case does not depend on
the client getting it right.

Everything here is a pure function over plain dicts — no database, no request —
so the rules can be tested directly, the way ``stage_rules`` and ``column_rules``
are.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# A picker, not a taxonomy. Past ~40 the dropdown is unusable and the reporting
# it feeds is meaningless, which is the actual reason for the cap.
MAX_REASONS = 40
MAX_LABEL_LENGTH = 60


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _key(label: str) -> str:
    """Duplicate detection is case- and space-insensitive.

    "No budget" and "no  budget" are the same reason to a human, and two of them
    in one dropdown is a reporting split nobody notices until the numbers are
    already wrong.
    """
    return _clean(label).casefold()


def _as_row(reason: Any) -> Dict[str, Any]:
    """Accept a LostReason model or a plain dict, return a plain dict."""
    if isinstance(reason, dict):
        get = reason.get
    else:
        def get(name, default=None):
            return getattr(reason, name, default)

    return {
        "id": get("id"),
        "label": _clean(get("label")),
        "order": get("order") or 0,
        "is_active": bool(get("is_active", True)),
    }


def _keep(row: Dict[str, Any], by_id: Dict[str, Any], used_ids: set) -> Any:
    """The row's own id if we already know it, otherwise None.

    A known id is preserved so a rename does not orphan the deals closed
    against that reason; anything else gets a fresh one rather than trusting a
    value the client invented. Each stored id binds at most once.
    """
    rid = str(row["id"]) if row["id"] is not None else None
    if rid in by_id and rid not in used_ids:
        used_ids.add(rid)
        return by_id[rid]["id"]
    return None


def merge_lost_reasons(
    existing: Optional[List[Any]],
    incoming: Optional[List[Any]],
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Fold the client's list into the stored one.

    ``incoming`` is the list AS THE USER ARRANGED IT — display order is list
    order. Returns ``(rows, None)`` or ``(None, message)``; the message is
    user-facing, because the route answers a business failure with HTTP 200 and
    ``status: "failure"``.

    Three things happen here that the caller must not have to remember:

    * a row carrying a known id KEEPS that id, so renaming a reason does not
      orphan the deals closed against it;
    * a stored row the client did not send is retired and appended, never
      dropped — see the module docstring;
    * ``order`` is renumbered contiguously from the incoming order, so the
      client never has to compute it and cannot send a broken sequence.
    """
    stored = [_as_row(r) for r in (existing or [])]
    by_id = {str(r["id"]): r for r in stored if r["id"] is not None}

    rows: List[Dict[str, Any]] = []
    seen_labels: Dict[str, str] = {}
    used_ids = set()

    for item in (incoming or []):
        row = _as_row(item)
        label = row["label"]

        # ⚠️ ONLY THE ACTIVE ROWS ARE VALIDATED, and the client agrees — see
        # `validate` in lostReasonHelpers.js.
        #
        # A retired reason is HISTORY. The screen renders it (greyed, with a
        # Restore button) and sends it back with the rest, so remove "Get
        # Different deal" and type it again and the payload legitimately holds
        # the retired row AND the new live one. Checking every row answered
        #     “Get Different deal” is in the list twice.
        # for a list whose picker showed it once — a save the user could not
        # complete and could not understand, because the offending row was the
        # one they had already removed.
        #
        # Refusing on a row nobody can pick is a dead end in the other cases
        # too: a blank or over-long label already in the collection would lock
        # the screen that exists to fix it.
        if not row["is_active"]:
            rows.append({
                "id": _keep(row, by_id, used_ids),
                "label": label,
                "order": len(rows),
                "is_active": False,
            })
            continue

        if not label:
            return None, "A lost reason cannot be blank."
        if len(label) > MAX_LABEL_LENGTH:
            return None, (f"“{label[:20]}…” is too long — "
                          f"keep a lost reason under {MAX_LABEL_LENGTH} characters.")

        key = _key(label)
        if key in seen_labels:
            return None, f"“{seen_labels[key]}” is in the list twice."
        seen_labels[key] = label

        rows.append({
            "id": _keep(row, by_id, used_ids),
            "label": label,
            "order": len(rows),
            "is_active": row["is_active"],
        })

    active_count = sum(1 for r in rows if r["is_active"])
    if active_count > MAX_REASONS:
        return None, (f"That is {active_count} reasons — "
                      f"a picker stops being usable past {MAX_REASONS}.")

    # ⚠️ THE OMITTED ONES ARE RETIRED, NOT DROPPED. A deal closed last quarter
    # still has to render its label, and the id is all it stored.
    for row in stored:
        if row["id"] is not None and str(row["id"]) in used_ids:
            continue
        rows.append({
            "id": row["id"],
            "label": row["label"],
            "order": len(rows),
            "is_active": False,
        })

    return rows, None


def default_rows() -> List[Dict[str, Any]]:
    """The shipped set, for a client offering to fill an empty list."""
    from app.services.defaults import DEFAULT_LOST_REASONS

    return [{"label": label, "order": i, "is_active": True}
            for i, label in enumerate(DEFAULT_LOST_REASONS)]
