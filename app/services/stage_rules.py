"""Pipeline structure invariants and stage-transition validation. CRM-013.

The riskiest logic in the CRM module: it decides whether a pipeline is
well-formed and whether a deal is allowed to move. Every rule lives here, in
one DB-free module, so it can be exhaustively unit-tested — this repo has no
database test harness, so a rule implemented inside a route handler is a rule
nobody can test.

Invariants enforced (CRM plan §6.2):

* 3 <= stages <= 20
* exactly one ``won`` and one ``lost`` stage, always the last two, in that
  order, never deletable
* stage names unique within a pipeline, case-insensitively
* ``order`` contiguous 0..n-1 after every mutation
* ``win_probability`` forced to 100 on won, 0 on lost
* exactly one default pipeline per account
* a pipeline holding deals cannot be deleted; the default never can

Error messages are user-facing sentences, not codes: the house convention
returns them at HTTP 200 with ``status="failure"`` and the UI renders them
verbatim.

Stages are plain dicts here, not Beanie models — that keeps the module free of
any DB import. The route layer converts.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

MIN_STAGES = 3
MAX_STAGES = 20

OPEN, WON, LOST = "open", "won", "lost"
_CLOSED = (WON, LOST)


class StageRuleError(ValueError):
    """A pipeline or stage-transition rule was violated.

    ``str(err)`` is safe to show the user as-is.
    """


def _type_of(stage: Dict[str, Any]) -> str:
    t = stage.get("type", OPEN)
    return getattr(t, "value", t) or OPEN


def _name_of(stage: Dict[str, Any]) -> str:
    return (stage.get("name") or "").strip()


def _sid(stage: Dict[str, Any]) -> str:
    return str(stage.get("id", stage.get("_id", "")))


# ── pipeline structure ───────────────────────────────────────────────────────
def validate_stages(stages: Sequence[Dict[str, Any]]) -> None:
    """Raise StageRuleError if the stage list is not well-formed."""
    n = len(stages)
    if n < MIN_STAGES:
        raise StageRuleError(f"A pipeline must have at least {MIN_STAGES} stages.")
    if n > MAX_STAGES:
        raise StageRuleError(f"A pipeline can have at most {MAX_STAGES} stages.")

    for s in stages:
        if not _name_of(s):
            raise StageRuleError("Every stage needs a name.")

    seen = set()
    for s in stages:
        key = _name_of(s).casefold()
        if key in seen:
            raise StageRuleError(
                f"Two stages are both called “{_name_of(s)}”. Stage names must be unique."
            )
        seen.add(key)

    won = [s for s in stages if _type_of(s) == WON]
    lost = [s for s in stages if _type_of(s) == LOST]
    if len(won) != 1:
        raise StageRuleError("A pipeline must have exactly one Won stage.")
    if len(lost) != 1:
        raise StageRuleError("A pipeline must have exactly one Lost stage.")

    if _type_of(stages[-2]) != WON or _type_of(stages[-1]) != LOST:
        raise StageRuleError("The Won and Lost stages must be the last two stages.")

    orders = [s.get("order") for s in stages]
    if orders != list(range(n)):
        raise StageRuleError("Stage order is not contiguous — reorder the stages.")

    for s in stages:
        p = s.get("win_probability", 0) or 0
        if not (0 <= p <= 100):
            raise StageRuleError("Win probability must be between 0 and 100.")


def apply_probability_rules(stages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Force won=100 / lost=0. Returns the same list, mutated in place."""
    for s in stages:
        t = _type_of(s)
        if t == WON:
            s["win_probability"] = 100
        elif t == LOST:
            s["win_probability"] = 0
    return stages


def first_open_stage(stages: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Where a deal lands when nobody chose a stage.

    ⚠️ SORTED BY ``order``, NOT ``stages[0]``. Every path that creates a deal
    has to agree on what "the first open stage" means, and they did not:
    ``routes/deals.py`` took the first OPEN entry in the stored array while
    ``routes/internal.py`` sorted by ``order`` first. ``normalise_stages``
    keeps the two in step for any pipeline mutated through these rules, so the
    divergence is invisible — until a pipeline arrives from an import, a seed
    script or a hand-edited document, and then one door puts the deal in New
    lead and the other in whatever happens to sit at index 0.

    ⚠️ AND ``_type_of``, NOT ``s.get("type") == "open"``. A stage whose
    ``type`` is absent or None is an OPEN stage — that is the model default —
    but the equality check skips it, so a pipeline carrying one such stage
    would report "no open stage" and refuse to create the deal at all.

    Returns None only when the pipeline genuinely has no open stage, which the
    caller must report rather than fall back on: a deal placed in Won or Lost
    at birth is closed before anyone sold anything.
    """
    def _field(stage: Any, key: str, default: Any = None) -> Any:
        # Accepts a dict OR an embedded `PipelineStage`: the deals route works
        # with `model_dump()`ed stages, the lead door and the import worker
        # with the model objects straight off the pipeline. One rule has to
        # serve all three or they drift apart again.
        if isinstance(stage, dict):
            value = stage.get(key, default)
        else:
            value = getattr(stage, key, default)
        return getattr(value, "value", value)

    ordered = sorted(stages, key=lambda st: _field(st, "order", 0) or 0)
    return next(
        (st for st in ordered if (_field(st, "type", OPEN) or OPEN) == OPEN),
        None,
    )


def normalise_stages(stages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pin Won/Lost last, renumber ``order`` contiguously, fix probabilities.

    Call this after any mutation, then ``validate_stages`` on the result.
    """
    open_stages = [s for s in stages if _type_of(s) == OPEN]
    won = [s for s in stages if _type_of(s) == WON]
    lost = [s for s in stages if _type_of(s) == LOST]
    ordered = open_stages + won + lost
    for i, s in enumerate(ordered):
        s["order"] = i
    return apply_probability_rules(ordered)


def reorder_open_stages(
    stages: Sequence[Dict[str, Any]], stage_ids: Sequence[Any]
) -> List[Dict[str, Any]]:
    """Apply a client-supplied order to the OPEN stages only.

    ``stage_ids`` must be the complete set of open-stage ids — a partial or
    duplicated list is a client bug and is rejected rather than guessed at.
    """
    open_stages = {_sid(s): s for s in stages if _type_of(s) == OPEN}
    wanted = [str(i) for i in stage_ids]

    if len(set(wanted)) != len(wanted):
        raise StageRuleError("The same stage appears twice in the new order.")
    if set(wanted) != set(open_stages):
        raise StageRuleError(
            "The new order must list every open stage exactly once."
        )

    closed = [s for s in stages if _type_of(s) in _CLOSED]
    return normalise_stages([open_stages[i] for i in wanted] + closed)


def insert_open_stage(
    stages: Sequence[Dict[str, Any]],
    new_stage: Dict[str, Any],
    after_stage_id: Any = None,
) -> List[Dict[str, Any]]:
    """Place a new OPEN stage, optionally after a given one.

    Extracted from the route so the positioning is testable: an off-by-one here
    silently puts a new stage in the wrong column and nobody notices until a
    board looks wrong.

    Raises:
        StageRuleError: ``after_stage_id`` is not an open stage of this pipeline.
    """
    open_rows = [s for s in stages if _type_of(s) == OPEN]
    closed_rows = [s for s in stages if _type_of(s) in _CLOSED]

    if after_stage_id is None:
        open_rows.append(new_stage)
    else:
        idx = next(
            (i for i, s in enumerate(open_rows) if _sid(s) == str(after_stage_id)), None
        )
        if idx is None:
            raise StageRuleError("The stage you asked to insert after does not exist.")
        open_rows.insert(idx + 1, new_stage)

    return normalise_stages(open_rows + closed_rows)


# Fields the Won and Lost stages will not accept. Their probabilities are fixed
# at 100/0, and a required-attribute rule on them could never fire because
# entering them goes through the close endpoint, not a stage change.
_CLOSED_STAGE_LOCKED_FIELDS = ("win_probability", "required_attributes", "rotting_days")


def apply_stage_edits(
    stages: Sequence[Dict[str, Any]], stage_id: Any, edits: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Apply a partial stage update, enforcing the Won/Lost restrictions.

    ``edits`` uses None to mean "not supplied" — matching the PATCH schema.

    Raises:
        StageRuleError: unknown stage, or a locked field on a closed stage.
    """
    rows = [dict(s) for s in stages]
    target = next((s for s in rows if _sid(s) == str(stage_id)), None)
    if target is None:
        raise StageRuleError("That stage does not exist in this pipeline.")

    supplied = {k: v for k, v in edits.items() if v is not None}
    if _type_of(target) in _CLOSED:
        locked = [f for f in _CLOSED_STAGE_LOCKED_FIELDS if f in supplied]
        if locked:
            raise StageRuleError(
                "The Won and Lost stages can only be renamed or recoloured."
            )

    for key, value in supplied.items():
        if key == "name":
            value = str(value).strip()
        target[key] = value

    return normalise_stages(rows)


def assemble_pipeline_stages(
    open_specs: Optional[Sequence[Dict[str, Any]]],
    closed_defaults: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build a new pipeline's stage list from caller-supplied OPEN stages.

    Won and Lost are never caller-supplied — they are appended from the
    defaults, so a client cannot create a pipeline with two Won stages or none.
    """
    open_rows = [
        {**spec, "type": OPEN} for spec in (open_specs or [])
        if _type_of(spec) == OPEN or "type" not in spec
    ]
    closed_rows = [s for s in closed_defaults if _type_of(s) in _CLOSED]
    return normalise_stages(open_rows + closed_rows)


def can_delete_stage(
    stages: Sequence[Dict[str, Any]],
    stage_id: Any,
    *,
    deal_count: int = 0,
    move_deals_to_stage_id: Any = None,
) -> None:
    """Raise StageRuleError if this stage may not be deleted."""
    target = next((s for s in stages if _sid(s) == str(stage_id)), None)
    if target is None:
        raise StageRuleError("That stage does not exist in this pipeline.")

    if _type_of(target) in _CLOSED:
        raise StageRuleError(
            "The Won and Lost stages cannot be deleted. You can rename them instead."
        )
    if len(stages) - 1 < MIN_STAGES:
        raise StageRuleError(f"A pipeline must have at least {MIN_STAGES} stages.")

    if deal_count > 0:
        if move_deals_to_stage_id is None:
            raise StageRuleError(
                f"“{_name_of(target)}” has {deal_count} "
                f"{'deal' if deal_count == 1 else 'deals'}. "
                "Choose another stage to move them to first."
            )
        dest = next(
            (s for s in stages if _sid(s) == str(move_deals_to_stage_id)), None
        )
        if dest is None:
            raise StageRuleError("The destination stage does not exist in this pipeline.")
        if _sid(dest) == str(stage_id):
            raise StageRuleError("Leads cannot be moved to the stage being deleted.")


def can_delete_pipeline(
    *, is_default: bool, deal_count: int = 0, migrate_to_pipeline_id: Any = None
) -> None:
    """Raise StageRuleError if this pipeline may not be deleted.

    Deliberate divergence from Brevo, which deletes a pipeline's deals
    irreversibly behind a single confirm dialog.
    """
    if is_default:
        raise StageRuleError(
            "Set another pipeline as the default before deleting this one."
        )
    if deal_count > 0 and migrate_to_pipeline_id is None:
        raise StageRuleError(
            f"This pipeline has {deal_count} "
            f"{'deal' if deal_count == 1 else 'deals'}. "
            "Move them to another pipeline first."
        )


def validate_single_default(pipelines: Iterable[Dict[str, Any]]) -> None:
    """Exactly one default pipeline per account — never zero, never two."""
    defaults = [p for p in pipelines if p.get("is_default")]
    if len(defaults) == 0:
        raise StageRuleError("One pipeline must be set as the default.")
    if len(defaults) > 1:
        raise StageRuleError("Only one pipeline can be the default.")


# ── deal transitions ─────────────────────────────────────────────────────────
def missing_required_attributes(
    deal: Dict[str, Any],
    stage: Dict[str, Any],
    *,
    labels: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Which of the target stage's required attributes are unset on this deal?

    Returns human-readable *labels*, ready to put in a message and in the
    ``missing_attributes`` payload. The UI renders exactly this list — it must
    never compute its own, or the two can disagree.

    A value counts as set unless it is None, an empty string, or an empty
    list/dict. Zero and False are legitimate values and count as set.
    """
    labels = labels or {}
    required = stage.get("required_attributes") or []
    custom = deal.get("custom_fields") or {}
    missing: List[str] = []

    for name in required:
        value = deal.get(name, custom.get(name))
        empty = value is None or (isinstance(value, (str, list, dict, tuple)) and len(value) == 0)
        if empty:
            missing.append(labels.get(name, _humanise(name)))
    return missing


def missing_required_pairs(
    deal: Dict[str, Any],
    stage: Dict[str, Any],
    *,
    labels: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """The same answer as ``missing_required_attributes``, as
    ``{internal_name, label}`` pairs.

    CRM-210. Bare labels are enough to *say* what is missing but not to render
    an input for it — the client needs the name to build the field and to send
    the value back. Matching the label back to a field client-side is what the
    Phase 1 dialog did, and it breaks the moment an account renames the
    attribute.

    The label-only function is kept because the user-facing sentence in
    ``validate_transition`` is built from it.
    """
    labels = labels or {}
    required = stage.get("required_attributes") or []
    custom = deal.get("custom_fields") or {}
    out: List[Dict[str, str]] = []

    for name in required:
        value = deal.get(name, custom.get(name))
        empty = value is None or (isinstance(value, (str, list, dict, tuple)) and len(value) == 0)
        if empty:
            out.append({"internal_name": str(name),
                        "label": labels.get(name, _humanise(name))})
    return out


def _humanise(internal_name: str) -> str:
    """``expected_close_date`` -> ``Expected close date``."""
    words = str(internal_name).replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else internal_name


def validate_transition(
    deal: Dict[str, Any],
    stage: Dict[str, Any],
    *,
    pipeline_stage_ids: Optional[Sequence[Any]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> None:
    """Raise StageRuleError if this deal may not enter this stage.

    Does NOT decide *who* may move it — that is ``permissions.can_modify``.
    """
    if pipeline_stage_ids is not None and _sid(stage) not in {
        str(i) for i in pipeline_stage_ids
    }:
        raise StageRuleError("That stage does not belong to the selected pipeline.")

    if _type_of(stage) in _CLOSED:
        raise StageRuleError(
            "Use the close action to mark a lead as won or lost, so the amount, "
            "close date and reason are recorded."
        )

    missing = missing_required_attributes(deal, stage, labels=labels)
    if missing:
        raise StageRuleError(
            f"Deal stage “{_name_of(stage)}” requires: {', '.join(missing)}."
        )


def status_for_stage(stage: Dict[str, Any]) -> str:
    """The ``status`` to denormalise onto the deal from its new stage."""
    return _type_of(stage)


def _as_utc(value):
    """Coerce a datetime to timezone-aware UTC.

    THIS IS NOT DEFENSIVE PADDING — it is the difference between a working
    board and a 500.

    The app builds its Motor client without ``tz_aware=True``
    (``app/__init__.py::_create_db_client``), so **PyMongo returns naive
    datetimes**: the value is UTC, the tzinfo is gone. Meanwhile every
    timestamp this module is handed from a route is
    ``datetime.now(timezone.utc)`` — aware. Subtracting one from the other
    raises ``TypeError: can't subtract offset-naive and offset-aware
    datetimes``.

    A naive value read back from Mongo is UTC by construction, so attaching
    UTC is a restoration, not a guess.
    """
    if value is None or not hasattr(value, "tzinfo"):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def days_in_stage(entered_at, now) -> int:
    """Whole days between two datetimes, floored at 0.

    Tolerates either side being naive — see ``_as_utc``.
    """
    if entered_at is None or now is None:
        return 0
    delta = _as_utc(now) - _as_utc(entered_at)
    return max(0, int(delta.total_seconds() // 86400))


def is_rotting(stage: Dict[str, Any], entered_at, now) -> bool:
    """Has this deal sat in this stage past the stage's rotting threshold?

    Only open stages rot — a won deal is not stale, it is finished.
    """
    if _type_of(stage) != OPEN:
        return False
    limit = stage.get("rotting_days")
    if not limit:
        return False
    return days_in_stage(entered_at, now) > int(limit)


def rotting_cutoff(stage: Dict[str, Any], now) -> Optional[Any]:
    """The newest ``stage_entered_at`` that still counts as rotting. CRM-411.

    ``is_rotting`` above is a per-record predicate; a list filter has to say the
    same thing in one query, so this gives the boundary a Mongo range can use::

        {"stage_id": stage_id, "stage_entered_at": {"$lte": rotting_cutoff(...)}}

    ⚠️ THE OFF-BY-ONE IS THE WHOLE POINT. ``is_rotting`` tests
    ``days_in_stage(entered_at, now) > limit`` where ``days_in_stage`` FLOORS to
    whole days. So a deal is rotting once the elapsed time reaches
    ``limit + 1`` whole days, not ``limit``:

        floor(delta / 86400) > limit   ⟺   delta >= (limit + 1) * 86400

    Compiling ``now - limit days`` instead would make the filter disagree with
    the badge on the card by exactly one day — the kind of divergence nobody
    reports as a bug, they just stop trusting the filter.

    Returns ``None`` when the stage cannot rot at all: a closed stage (a won
    deal is not stale, it is finished) or no threshold set.
    """
    if _type_of(stage) != OPEN:
        return None
    limit = stage.get("rotting_days")
    if not limit:
        return None
    return _as_utc(now) - timedelta(days=int(limit) + 1)
