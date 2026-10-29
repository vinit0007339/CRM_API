"""Pipeline reporting — where deals die, how long they sit, who converts.

Four read-only endpoints. Nothing here writes, and nothing here is new data:
`app/services/reporting.py`'s module docstring lists what was already being
recorded and by what.

⚠️ GATED ON `deals:read`, NOT A NEW PERMISSION. A report is a view of deals, so
inventing `reports:read` would mean a permission no role grants — and
`scripts/check_invariants.py` already reports eight of those, each one an
endpoint nobody can reach by default. It also keeps `rbac.PERMISSIONS` at 32,
so the pinned set does not move.

⚠️ THERE IS NO ARCHIVED-DEALS QUESTION, AND THAT SURPRISED ME. The plan for
this feature said to "decide explicitly whether archived deals count, because a
conversion rate that silently drops them reads better than reality". It turns
out **a deal cannot be archived**: `DealModel` has no `is_archived` field and
`bulk_rules.DEAL_ACTIONS` has no `archive` — companies and contacts have both,
deals have neither. A deal leaves the pipeline by being won, lost or deleted.

So the `include_archived` parameter these endpoints first carried was a no-op
that implied otherwise, and its `is_archived: False` filter matched **nothing**
— every report returned zero deals. Removed rather than kept as a courtesy: a
parameter that cannot change the answer is a worse lie than no parameter.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app.config import logger as _logger
from app.crud import (
    CrmActivityCrud, CrmSettingsCrud, DealCrud, PipelineCrud,
)
from app.crud.dependency import get_crud
from app.object_id import PyObjectId
from app.auth.context import RequestContext, require
from app.schemas.common import ResponseSchema
from app.services import forecast, reporting

router = APIRouter()

OBJECT = "deal"

#: ⚠️ A CAP, BECAUSE THIS READS A WHOLE PIPELINE'S HISTORY. An account with
#: forty thousand deals would otherwise pull every one of them and every stage
#: change they ever had into memory to compute an average. The reports say when
#: they hit it rather than quietly reporting on a slice — a truncated
#: conversion rate that does not admit it is worse than no conversion rate.
MAX_DEALS = 5000
MAX_ACTIVITIES = 50000


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _fail(message: str, data: Any = None) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=data)


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _gather(pipelines, deals_crud, activity, account_id: PyObjectId,
                  pipeline_id: PyObjectId, owner_id: Optional[PyObjectId],
                  created_from: Optional[datetime],
                  created_to: Optional[datetime]):
    """The pipeline, its deals and their stage-change activities.

    One place, because all four endpoints need the same three things and a
    second copy of this filter is a second chance for two reports to disagree
    about which deals they counted.
    """
    pipeline = await pipelines.get_scoped(account_id, pipeline_id)
    if pipeline is None:
        return None, None, None, _fail("That pipeline no longer exists.")

    query: Dict[str, Any] = {"created_by": account_id,
                             "pipeline_id": pipelines.oid(pipeline_id)}
    if owner_id:
        query["owner_id"] = deals_crud.oid(owner_id)
    window: Dict[str, Any] = {}
    if created_from:
        window["$gte"] = created_from
    if created_to:
        window["$lte"] = created_to
    if window:
        # ⚠️ ON `created_at`, NOT ON THE CLOSE DATE. The question these reports
        # answer is "of the deals that ENTERED the pipeline in this window,
        # what happened to them" — a cohort. Filtering on the close date
        # instead would count a deal created in January and won in March as a
        # March deal, and the conversion rate of a month would depend on how
        # long deals take, which is the other report.
        query["created_at"] = window

    deals = await deals_crud.list_by(query, limit=MAX_DEALS)
    deal_ids = [d.id for d in deals]

    activities: List[Any] = []
    if deal_ids:
        activities = await activity.list_by(
            {"created_by": account_id, "entity_type": OBJECT,
             "entity_id": {"$in": deal_ids},
             # ⚠️ BOTH MOVEMENT TYPES. `reopen` records `"reopened"`, not
             # `"stage_changed"`, so querying one type loses every re-entry —
             # `reporting.MOVEMENT_TYPES` is the set the aggregations read.
             "activity_type": {"$in": list(reporting.MOVEMENT_TYPES)}},
            limit=MAX_ACTIVITIES)
    return pipeline, deals, activities, None


def _scope(pipeline, deals, activities,
           owner_id, created_from, created_to) -> Dict[str, Any]:
    """What the numbers below were computed over. Always returned."""
    return {
        "pipeline_id": str(pipeline.id),
        "pipeline_name": pipeline.name,
        "deals": len(deals),
        "owner_id": str(owner_id) if owner_id else None,
        "created_from": created_from,
        "created_to": created_to,
        # ⚠️ SAY SO WHEN THE CAP WAS HIT. A report quietly computed over the
        # first 5,000 of 12,000 deals is a wrong number that looks right.
        "truncated_deals": len(deals) >= MAX_DEALS,
        "truncated_activities": len(activities) >= MAX_ACTIVITIES,
    }


def _stages(pipeline) -> List[Dict[str, Any]]:
    out = []
    for stage in (pipeline.stages or []):
        out.append({
            "id": str(getattr(stage, "id", None) or ""),
            "name": getattr(stage, "name", None),
            "order": int(getattr(stage, "order", 0) or 0),
            "type": getattr(getattr(stage, "type", None), "value",
                            getattr(stage, "type", None)),
            "win_probability": getattr(stage, "win_probability", None),
        })
    return out


# ── the four reports ─────────────────────────────────────────────────────────

@router.get("/pipeline", response_model=ResponseSchema)
async def pipeline_report(
    pipeline_id: PyObjectId = Query(...),
    owner_id: Optional[PyObjectId] = Query(None),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    ctx: RequestContext = Depends(require("deals:read")),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    deals_crud: DealCrud = Depends(get_crud(DealCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Headline health: open / won / lost counts and value, and the weighted total.

    The weighted figure comes from `forecast.py` rather than a second
    calculation — it owns stage probability and the per-deal override, and two
    answers to "what is the pipeline worth" on the same screen is the bug this
    avoids.
    """
    account_id = _account_id(ctx)
    pipeline, deals, activities, error = await _gather(
        pipelines, deals_crud, activity, account_id, pipeline_id, owner_id,
        created_from, created_to)
    if error:
        return error

    stages = _stages(pipeline)
    by_status: Dict[str, Dict[str, Any]] = {
        s: {"deals": 0, "value": 0.0} for s in ("open", "won", "lost")}
    for deal in deals:
        status = getattr(getattr(deal, "status", None), "value",
                         getattr(deal, "status", "open")) or "open"
        if status in by_status:
            by_status[status]["deals"] += 1
            by_status[status]["value"] += float(getattr(deal, "amount", 0) or 0)

    open_deals = [d for d in deals
                  if (getattr(getattr(d, "status", None), "value",
                              getattr(d, "status", "open")) or "open") == "open"]
    stage_index = {s["id"]: s for s in stages}
    weighted = forecast.weighted_total(
        [{"amount": getattr(d, "amount", None),
          "probability": getattr(d, "probability", None),
          "stage_id": str(getattr(d, "stage_id", "") or "")} for d in open_deals],
        stage_index) if open_deals else 0.0

    return _ok({
        "scope": _scope(pipeline, deals, activities,
                        owner_id, created_from, created_to),
        "by_status": by_status,
        "weighted_open_value": round(float(weighted or 0), 2),
        "win_rate_by_owner": reporting.win_rate_by_owner(deals),
    })


@router.get("/conversion", response_model=ResponseSchema)
async def conversion_report(
    pipeline_id: PyObjectId = Query(...),
    owner_id: Optional[PyObjectId] = Query(None),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    ctx: RequestContext = Depends(require("deals:read")),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    deals_crud: DealCrud = Depends(get_crud(DealCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Per stage: entered, advanced, lost here, and the conversion rate."""
    account_id = _account_id(ctx)
    pipeline, deals, activities, error = await _gather(
        pipelines, deals_crud, activity, account_id, pipeline_id, owner_id,
        created_from, created_to)
    if error:
        return error
    rows, unattributed = reporting.conversion(
        _stages(pipeline), deals, activities)
    return _ok({
        "scope": _scope(pipeline, deals, activities,
                        owner_id, created_from, created_to),
        "stages": rows,
        # Closed deals whose death stage is not in the data. Surfaced, not
        # folded into stage one — see `reporting.conversion`.
        "unattributed": unattributed,
    })


@router.get("/time-in-stage", response_model=ResponseSchema)
async def time_in_stage_report(
    pipeline_id: PyObjectId = Query(...),
    owner_id: Optional[PyObjectId] = Query(None),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    ctx: RequestContext = Depends(require("deals:read")),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    deals_crud: DealCrud = Depends(get_crud(DealCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Per stage: average, median and longest dwell in days.

    Includes deals still sitting in a stage, via `stage_entered_at` — see
    `reporting.time_in_stage` for why reporting only completed spells makes the
    slowest stage look like the fastest.
    """
    account_id = _account_id(ctx)
    pipeline, deals, activities, error = await _gather(
        pipelines, deals_crud, activity, account_id, pipeline_id, owner_id,
        created_from, created_to)
    if error:
        return error
    return _ok({
        "scope": _scope(pipeline, deals, activities,
                        owner_id, created_from, created_to),
        "stages": reporting.time_in_stage(_stages(pipeline), deals,
                                          activities, _utcnow()),
    })


@router.get("/lost-reasons", response_model=ResponseSchema)
async def lost_reasons_report(
    pipeline_id: PyObjectId = Query(...),
    owner_id: Optional[PyObjectId] = Query(None),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    ctx: RequestContext = Depends(require("deals:read")),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    deals_crud: DealCrud = Depends(get_crud(DealCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    settings_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Why deals were lost, by count and by value.

    The only report that reads `crm_settings`: `lost_reasons` has been
    collected per account since the beginning and nothing has ever read it
    back. `unattributed` is lost deals with no reason, which is the number that
    says whether the rest of this report can be trusted.
    """
    account_id = _account_id(ctx)
    pipeline, deals, activities, error = await _gather(
        pipelines, deals_crud, activity, account_id, pipeline_id, owner_id,
        created_from, created_to)
    if error:
        return error

    configured: List[Any] = []
    try:
        settings_row = await settings_crud.get_or_create(account_id=account_id)
        configured = list(getattr(settings_row, "lost_reasons", None) or [])
    except Exception as ex:                      # pylint: disable=broad-except
        # Degrade to the reasons the DEALS name rather than failing: a report
        # that refuses because settings could not be read tells the reader
        # nothing, where one missing its labels still shows the distribution.
        _logger.exception(ex)

    rows, unattributed = reporting.lost_reasons(deals, configured)
    return _ok({
        "scope": _scope(pipeline, deals, activities,
                        owner_id, created_from, created_to),
        "reasons": rows,
        "unattributed": unattributed,
        "labels_available": bool(configured),
    })
