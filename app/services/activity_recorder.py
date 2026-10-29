"""Builds CRM timeline entries. CRM-014.

Every mutation to a company or deal produces exactly one activity, and every
activity is built here — never assembled inline in a route handler. That is
what keeps titles consistent and makes the diffing testable.

This module is PURE: it returns payload dicts and lets the caller persist them.
``record()`` takes a ``writer`` callable so the route/CRUD layer owns the
database round-trip and this module stays unit-testable without one.

Titles are rendered server-side and stored. Two reasons: the timeline UI then
does no string assembly, and a historical entry keeps its original wording even
after a stage or attribute is renamed later.

**This must ship in Phase 1** even though reporting is Phase 4. Stage-duration
analysis can only read history that was recorded from day one — deals created
before the recorder exists have no history to report on, permanently.

Do NOT route these through the ``events/`` package; it is broken (see
``app/db/models/main/crm/activity.py``).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# A single "field updated" activity carries at most this many changes. A bulk
# edit touching 200 fields should not write a 200-entry metadata blob.
MAX_DIFF_FIELDS = 20

# Never diffed: churn, internal bookkeeping, or already covered by a dedicated
# activity type.
_IGNORED_FIELDS = frozenset({
    "updated_at", "created_at", "last_activity_at", "activity_count",
    "position", "stage_entered_at", "created_by", "created_by_user_id",
    "workspace_id", "contact_count", "deal_count", "open_deal_value",
    "won_deal_value", "open_task_count", "overdue_task_count",
    "next_activity_at", "primary_company_name",
})


def _humanise(name: str) -> str:
    """``expected_close_date`` -> ``Expected close date``."""
    words = str(name).replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else str(name)


def diff_fields(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    *,
    ignore: Iterable[str] = (),
    max_fields: int = MAX_DIFF_FIELDS,
) -> Dict[str, Dict[str, Any]]:
    """``{field: {"from": old, "to": new}}`` for genuinely changed fields.

    Only keys present in ``after`` are considered, so a partial update payload
    doesn't read as "everything else was cleared".
    """
    before = before or {}
    after = after or {}
    skip = _IGNORED_FIELDS | set(ignore)

    changes: Dict[str, Dict[str, Any]] = {}
    for key, new in after.items():
        if key in skip:
            continue
        old = before.get(key)
        if old == new:
            continue
        changes[key] = {"from": old, "to": new}
        if len(changes) >= max_fields:
            break
    return changes


def build_activity(
    *,
    account_id: Any,
    entity_type: str,
    entity_id: Any,
    activity_type: str,
    title: str,
    body: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    actor_user_id: Any = None,
    actor_name: Optional[str] = None,
    source: str = "ui",
    occurred_at: Any = None,
) -> Dict[str, Any]:
    """The canonical activity payload. Shape matches ``CrmActivityModel``."""
    payload: Dict[str, Any] = {
        "created_by": account_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "activity_type": activity_type,
        "actor_user_id": actor_user_id,
        "actor_name": actor_name,
        "source": source,
        "title": title,
        "body": body,
        "metadata": metadata or {},
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    return payload


# ── typed builders ───────────────────────────────────────────────────────────
# ⚠️ A LOOKUP, NOT `"Company" if entity_type == "company" else "Deal"`.
# That spelling labelled EVERY other object type a Deal, so a contact created
# by CRM-412's import wrote "Deal created" into its own timeline. It is the
# fifth instance of this exact pattern in the module — Phase 6 found four at
# once in `attributes.py`, `defaults.py`, `column_rules.py` and the bulk
# runner. An unknown type now falls back to a generic word rather than
# borrowing another object's noun.
#: ⚠️ THE VALUES ARE WHAT A PERSON READS IN THE TIMELINE; THE KEYS ARE
#: ENTITY TYPES AND MUST NOT MOVE. The module is called Leads in the UI,
#: so an activity row that says "Deal linked" names something the user
#: cannot find.
ENTITY_LABELS = {"company": "Company", "deal": "Lead", "contact": "Contact"}

# `created()`'s own vocabulary -> `ActivitySource` members.
SOURCE_FOR_MODEL = {"manual": "ui"}


def created(*, entity_type: str, name: str, source: str = "manual",
            **kw) -> Dict[str, Any]:
    label = ENTITY_LABELS.get(entity_type, "Record")
    return build_activity(
        entity_type=entity_type,
        activity_type="created",
        title=f"{label} created",
        metadata={"name": name, "source": source},
        # ⚠️ FORWARDED TO THE ACTIVITY'S OWN `source`, not only buried in
        # metadata. `CrmActivityModel.source` defaults to UI, so every
        # import-created record claimed to have been typed in by hand — and
        # `ActivitySource` exists precisely so a bulk or import side effect can
        # be told apart from a person's action. CRM-417's event contract
        # depends on the same distinction.
        #
        # ⚠️ "manual" IS NOT AN `ActivitySource` MEMBER (ui/api/import/
        # automation/system). It is this function's own default, meaning "a
        # person did this", and it stays in `metadata` for display while the
        # model field gets the member that means the same thing. Passing it
        # straight through would write a value the enum does not have.
        source=SOURCE_FOR_MODEL.get(source, source),
        **kw,
    )


# ── notes ────────────────────────────────────────────────────────────────────
NOTE_TITLES = {
    "note_added": "Note added",
    "note_updated": "Note edited",
    "note_deleted": "Note deleted",
}


def note_activities(*, kind: str, note_id: Any, preview: Optional[str],
                    targets: Iterable[Tuple[str, Iterable[Any]]],
                    **kw) -> List[Dict[str, Any]]:
    """One activity per attached record, so a note shows on every timeline.

    ⚠️ BUILT HERE, NOT IN THE ROUTE. ``POST /crm/notes`` and the note importer
    (CRM-413) both write these rows, and the import's first draft built its
    own — which is how one path comes to title a row "Note added" while the
    other says "Note imported", and how ``source`` stays "ui" on a row no
    person typed. One builder, two callers; ``source`` is the caller's to pass.

    The preview is PLAIN TEXT. An activity body is rendered as text everywhere
    it appears, so putting HTML in one would either show markup or invite a
    consumer to render it — which would undo the sanitiser's whole reason for
    existing.
    """
    return [
        build_activity(
            entity_type=entity_type,
            entity_id=entity_id,
            activity_type=kind,
            title=NOTE_TITLES.get(kind, "Note"),
            body=preview,
            metadata={"note_id": str(note_id)},
            **kw,
        )
        for entity_type, ids in targets
        for entity_id in (ids or [])
    ]


def updated(
    *, entity_type: str, before: Dict[str, Any], after: Dict[str, Any], **kw
) -> Optional[Dict[str, Any]]:
    """None when nothing actually changed — don't write an empty activity."""
    changes = diff_fields(before, after)
    if not changes:
        return None
    if len(changes) == 1:
        field = next(iter(changes))
        title = f"{_humanise(field)} updated"
    else:
        title = f"{len(changes)} fields updated"
    return build_activity(
        entity_type=entity_type,
        activity_type="updated",
        title=title,
        metadata={"changes": changes},
        **kw,
    )


def stage_changed(
    *,
    from_stage: Optional[Dict[str, Any]],
    to_stage: Dict[str, Any],
    days_in_previous_stage: int = 0,
    **kw,
) -> Dict[str, Any]:
    """The activity that makes stage-duration reporting possible."""
    from_name = (from_stage or {}).get("name")
    to_name = to_stage.get("name")
    title = (
        f"Moved from {from_name} to {to_name}" if from_name else f"Moved to {to_name}"
    )
    return build_activity(
        entity_type="deal",
        activity_type="stage_changed",
        title=title,
        metadata={
            "from_stage_id": (from_stage or {}).get("id"),
            "from_stage_name": from_name,
            "to_stage_id": to_stage.get("id"),
            "to_stage_name": to_name,
            "days_in_previous_stage": days_in_previous_stage,
        },
        **kw,
    )


def closed(
    *,
    outcome: str,
    amount: Any = None,
    close_date: Any = None,
    reason: Optional[str] = None,
    note: Optional[str] = None,
    **kw,
) -> Dict[str, Any]:
    title = "Lead won" if outcome == "won" else "Lead lost"
    if outcome == "lost" and reason:
        title = f"Lead lost — {reason}"
    return build_activity(
        entity_type="deal",
        activity_type=outcome,
        title=title,
        body=note,
        metadata={"amount": amount, "close_date": close_date, "lost_reason": reason},
        **kw,
    )


def reopened(*, to_stage: Dict[str, Any], **kw) -> Dict[str, Any]:
    return build_activity(
        entity_type="deal",
        activity_type="reopened",
        title=f"Lead reopened in {to_stage.get('name')}",
        metadata={"to_stage_id": to_stage.get("id"), "to_stage_name": to_stage.get("name")},
        **kw,
    )


def owner_changed(
    *, entity_type: str, from_name: Optional[str], to_name: Optional[str], **kw
) -> Dict[str, Any]:
    title = f"Owner changed to {to_name}" if to_name else "Owner cleared"
    return build_activity(
        entity_type=entity_type,
        activity_type="owner_changed",
        title=title,
        metadata={"from": from_name, "to": to_name},
        **kw,
    )


def linked(
    *,
    entity_type: str,
    other_type: str,
    other_name: Optional[str],
    other_id: Any,
    unlink: bool = False,
    **kw,
) -> Dict[str, Any]:
    verb = "unlinked from" if unlink else "linked to"
    action = f"{other_type}_{'unlinked' if unlink else 'linked'}"
    return build_activity(
        entity_type=entity_type,
        activity_type=action,
        title=f"{other_name or other_type.title()} {verb} this {entity_type}",
        metadata={"entity_id": other_id, "entity_name": other_name},
        **kw,
    )


# ── parent-record side effects ───────────────────────────────────────────────
def parent_touch(occurred_at: Any) -> Dict[str, Any]:
    """The ``$set``/``$inc`` the parent record needs alongside an activity.

    Applied in the same operation as the activity write so the two cannot
    drift apart.
    """
    return {
        "$set": {"last_activity_at": occurred_at, "updated_at": occurred_at},
        "$inc": {"activity_count": 1},
    }


def record(
    writer: Callable[[Dict[str, Any]], Any], activity: Optional[Dict[str, Any]]
) -> Optional[Any]:
    """Persist one activity via the caller's writer. No-op for None."""
    if activity is None:
        return None
    return writer(activity)


def record_bulk(
    writer: Callable[[List[Dict[str, Any]]], Any],
    activities: Iterable[Optional[Dict[str, Any]]],
) -> Optional[Any]:
    """One aggregate activity per affected record — never one per field."""
    payload = [a for a in activities if a is not None]
    if not payload:
        return None
    return writer(payload)
