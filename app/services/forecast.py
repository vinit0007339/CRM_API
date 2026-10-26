"""Weighted pipeline forecasting. CRM-018.

``weighted_value = SUM(amount * probability / 100)`` where probability is the
deal's own override if it has one, otherwise its stage's ``win_probability``.

Two rules that are easy to get wrong and are tested explicitly:

* a deal with no amount contributes **zero**, it is not an error and it is not
  skipped from the count;
* rounding happens **once, at the end** of a sum — never per deal — otherwise
  a 500-deal column drifts by several rupees from the board total.

Stdlib-only.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

# Currency amounts are reported to 2dp. Intermediate sums stay unrounded.
_PRECISION = 2


def deal_probability(deal: Dict[str, Any], stage: Optional[Dict[str, Any]] = None) -> int:
    """The probability to apply to one deal, 0-100.

    A per-deal override wins over the stage default. Won/lost stages are
    absolute: 100 and 0 respectively, regardless of any stale override.
    """
    stage = stage or {}
    stage_type = stage.get("type", "open")
    stage_type = getattr(stage_type, "value", stage_type)
    if stage_type == "won":
        return 100
    if stage_type == "lost":
        return 0

    override = deal.get("probability")
    if override is not None:
        return max(0, min(100, int(override)))
    return max(0, min(100, int(stage.get("win_probability") or 0)))


def weighted_amount(deal: Dict[str, Any], stage: Optional[Dict[str, Any]] = None) -> float:
    """One deal's contribution to the forecast. Unrounded by design."""
    amount = deal.get("amount")
    if amount is None:
        return 0.0
    return float(amount) * deal_probability(deal, stage) / 100.0


def total_value(deals: Iterable[Dict[str, Any]]) -> float:
    """Plain sum of amounts, treating a missing amount as zero."""
    return round(sum(float(d.get("amount") or 0.0) for d in deals), _PRECISION)


def weighted_total(
    deals: Iterable[Dict[str, Any]], stage: Optional[Dict[str, Any]] = None
) -> float:
    """Weighted sum. Rounded once, here."""
    return round(sum(weighted_amount(d, stage) for d in deals), _PRECISION)


def column_weighted_value(
    stage: Dict[str, Any],
    *,
    weighted_overridden: float = 0.0,
    amount_without_override: float = 0.0,
) -> float:
    """An OPEN column's weighted value, from the aggregation's split sums.

    ``weighted_overridden`` is already weighted per deal — those deals carry
    their own ``probability``. ``amount_without_override`` is raw amount and
    takes the stage's ``win_probability``.

    This is the aggregate twin of ``weighted_amount``: over the same set of
    deals the two must agree, or a column header disagrees with the sum of the
    cards visible beneath it. Won and Lost do not come through here — see
    ``stage_weighted_from_aggregate``.
    """
    pct = max(0, min(100, int(stage.get("win_probability") or 0)))
    return round(
        float(weighted_overridden) + float(amount_without_override) * pct / 100.0,
        _PRECISION,
    )


def _stage_weighted_raw(stage: Dict[str, Any], agg: Dict[str, Any]) -> float:
    """``stage_weighted_from_aggregate`` WITHOUT the rounding.

    ⚠️ ROUNDING ONCE IS A RULE OF THIS MODULE, and summing rounded
    per-stage figures breaks it. A list spanning twenty stages drifts by up to
    ten paise from the board's own total for the same deals, and the only
    symptom is two screens quietly disagreeing. So the per-stage figure a
    COLUMN HEADER shows is rounded (it is displayed), and the figure a SUM
    consumes is not.
    """
    stage_type = stage.get("type", "open")
    stage_type = getattr(stage_type, "value", stage_type)
    if stage_type == "won":
        return float(agg.get("total_value") or 0.0)
    if stage_type == "lost":
        return 0.0
    pct = max(0, min(100, int(stage.get("win_probability") or 0)))
    return (float(agg.get("weighted_overridden") or 0.0)
            + float(agg.get("amount_without_override") or 0.0) * pct / 100.0)


def stage_weighted_from_aggregate(stage: Dict[str, Any], agg: Dict[str, Any]) -> float:
    """The weighted figure a Kanban column header shows.

    Closed stages are absolute and ignore any stale per-deal override, matching
    ``deal_probability``: Won counts in full, Lost counts for nothing.

    Rounded, because it is displayed. A caller SUMMING these wants
    ``_stage_weighted_raw`` instead.
    """
    return round(_stage_weighted_raw(stage, agg), _PRECISION)


def stage_totals(stage: Dict[str, Any], deals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The three numbers a Kanban column header shows."""
    return {
        "deal_count": len(deals),
        "total_value": total_value(deals),
        "weighted_value": weighted_total(deals, stage),
    }


def board_totals(stage_summaries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll per-stage summaries up to the board header.

    Sums the already-rounded stage figures so the header always equals the
    visible columns — a user adding up the columns must not get a different
    number from the one we print.
    """
    summaries = list(stage_summaries)
    return {
        "deal_count": sum(int(s.get("deal_count") or 0) for s in summaries),
        "total_value": round(sum(float(s.get("total_value") or 0.0) for s in summaries), _PRECISION),
        "weighted_value": round(sum(float(s.get("weighted_value") or 0.0) for s in summaries), _PRECISION),
    }


def build_board_summary(
    stages: Sequence[Dict[str, Any]],
    deals_by_stage: Dict[Any, Sequence[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Per-stage summaries plus the board rollup, in stage order."""
    per_stage: List[Dict[str, Any]] = []
    for s in stages:
        sid = s.get("id", s.get("_id"))
        deals = deals_by_stage.get(sid, deals_by_stage.get(str(sid), []))
        summary = stage_totals(s, deals)
        summary["id"] = sid
        summary["name"] = s.get("name")
        per_stage.append(summary)
    return {"stages": per_stage, "totals": board_totals(per_stage)}


def list_summary(groups: Iterable[Dict[str, Any]],
                 stages_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The deals list's footer, from the per-stage aggregate. E24.

    ``groups`` is what ``DealCrud.list_totals`` returns — one row per stage
    over the WHOLE filtered set, not the page. Each row is weighted with its
    own stage, because a list spans pipelines and a single multiplier for all
    of them would be meaningless.

    ⚠️ A DEAL WHOSE STAGE WE CANNOT FIND IS COUNTED AND REPORTED, NOT WEIGHTED
    AND NOT DROPPED. Its pipeline may have been deleted. Treating it as 0%
    understates the forecast, treating it as 100% overstates it, and dropping
    it makes the count disagree with the list's own total. So it counts toward
    ``total_value`` and ``deal_count``, stays out of ``weighted_value``, and
    ``unweighted_count`` says how many — which is what lets the footer admit
    the weighted figure is incomplete instead of quietly being wrong.
    """
    deal_count = 0
    total = 0.0
    weighted = 0.0
    open_total = 0.0
    open_weighted = 0.0
    unweighted = 0

    for row in groups or []:
        count = int(row.get("deal_count") or 0)
        amount = float(row.get("total_value") or 0.0)
        deal_count += count
        total += amount

        stage = stages_by_id.get(str(row.get("stage_id") or row.get("_id") or ""))
        if not stage:
            unweighted += count
            continue

        # Unrounded: this is a SUM, and `_PRECISION` is applied once below.
        value = _stage_weighted_raw(stage, row)
        weighted += value

        stage_type = stage.get("type", "open")
        stage_type = getattr(stage_type, "value", stage_type)
        if stage_type == "open":
            open_total += amount
            open_weighted += value

    return {
        "deal_count": deal_count,
        "total_value": round(total, _PRECISION),
        "weighted_value": round(weighted, _PRECISION),
        # "Pipeline value" means OPEN deals everywhere in this UI; the plain
        # totals above include won and lost, which is what a filtered list
        # showing closed deals has to be able to add up.
        "open_total_value": round(open_total, _PRECISION),
        "open_weighted_value": round(open_weighted, _PRECISION),
        "unweighted_count": unweighted,
    }


def open_pipeline_value(
    deals: Iterable[Dict[str, Any]], stages_by_id: Dict[Any, Dict[str, Any]]
) -> Dict[str, float]:
    """Total and weighted value of OPEN deals only — what "pipeline value"
    means everywhere in the UI. Won and lost deals are excluded.
    """
    open_deals = []
    for d in deals:
        stage = stages_by_id.get(d.get("stage_id"), stages_by_id.get(str(d.get("stage_id")), {}))
        stage_type = stage.get("type", "open")
        stage_type = getattr(stage_type, "value", stage_type)
        if stage_type == "open":
            open_deals.append((d, stage))
    return {
        "total_value": round(sum(float(d.get("amount") or 0.0) for d, _ in open_deals), _PRECISION),
        "weighted_value": round(sum(weighted_amount(d, s) for d, s in open_deals), _PRECISION),
    }
