"""Pipeline & stage API. CRM-024 / 025 / 026 / 027.

Mounted at ``/api/v1/crm/pipelines``::

    GET    ""                              list (auto-provisions the default)
    POST   ""                              create
    GET    "/{pipeline_id}"                detail
    PATCH  "/{pipeline_id}"                update
    DELETE "/{pipeline_id}"                delete (refuses if it holds deals)
    POST   "/{pipeline_id}/stages"                    add a stage
    PATCH  "/{pipeline_id}/stages/{stage_id}"         update a stage
    DELETE "/{pipeline_id}/stages/{stage_id}"         delete a stage
    PUT    "/{pipeline_id}/stages/reorder"            reorder the open stages

Brevo exposes no pipeline or stage CRUD over its API at all — it is UI-only.
We expose it: it costs little and makes the module scriptable.

Every structural rule (3-20 stages, one Won + one Lost pinned last, unique
names, contiguous order, one default per account, deletion guards) lives in
``api/services/engage/crm/stage_rules.py`` — pure and unit-tested. These
handlers translate, call, and render; they never re-implement a rule.

House conventions followed here:
* business failures return **HTTP 200** with ``status="failure"`` so the UI
  renders the message inline (``StageRuleError`` carries a user-facing sentence);
* every stage mutation returns the **whole pipeline**, so the client always has
  a consistent stage array and never merges partial updates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.config import logger as _logger
from app.crud import PipelineCrud
from app.models.pipeline import PipelineModel, PipelineStage
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.pipeline import (
    PipelineCreateRequest,
    PipelineDetails,
    PipelineListResponse,
    PipelineUpdateRequest,
    StageCreateRequest,
    StageReorderRequest,
    StageUpdateRequest,
)
from app.services import defaults, stage_rules
from app.services.stage_rules import StageRuleError

router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _account_id(ctx: RequestContext) -> PyObjectId:
    """The tenant key. Team members act inside the *owner's* account, which is
    what makes CRM data shared across a team.
    """
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _fail(message: str) -> ResponseSchema:
    """Business failure: HTTP 200, ``status="failure"`` (house convention)."""
    return ResponseSchema(status="failure", message=message, data=None)


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _stages_to_dicts(pipeline: PipelineModel) -> List[Dict[str, Any]]:
    """stage_rules works on plain dicts so it can stay DB-free."""
    return [s.model_dump() for s in (pipeline.stages or [])]


def _dicts_to_stages(rows: List[Dict[str, Any]]) -> List[PipelineStage]:
    return [PipelineStage(**r) for r in rows]


def _details(pipeline: PipelineModel, deal_count: int = 0) -> PipelineDetails:
    payload = pipeline.model_dump(by_alias=True)
    payload["deal_count"] = deal_count
    return PipelineDetails.model_validate(payload)


async def _apply_stages(
    pipeline: PipelineModel, rows: List[Dict[str, Any]]
) -> PipelineModel:
    """Normalise → validate → persist. The only path that writes stages."""
    rows = stage_rules.normalise_stages(rows)
    stage_rules.validate_stages(rows)          # raises StageRuleError
    pipeline.stages = _dicts_to_stages(rows)
    pipeline.updated_at = _utcnow()
    await pipeline.save()
    return pipeline


async def _load(crud: PipelineCrud, account_id: Any, pipeline_id: Any
                ) -> Optional[PipelineModel]:
    return await crud.get_scoped(account_id, pipeline_id)


# ── CRM-024 · list + auto-provision ──────────────────────────────────────────
@router.get("", response_model=PipelineListResponse)
async def list_pipelines(
    include_archived: bool = Query(False),
    ctx: RequestContext = Depends(require("pipelines:read")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """List pipelines, creating the default one if the account has none.

    A brand-new account must never see an empty CRM, and the client must never
    have to special-case "no pipelines yet". Provisioning is idempotent: a
    second call finds the pipeline created by the first.
    """
    account_id = _account_id(ctx)
    pipelines = await crud.list(account_id=account_id, include_archived=include_archived)

    if not pipelines:
        created = await _provision_default(crud, account_id, ctx)
        pipelines = [created] if created else []

    data = []
    for p in pipelines:
        count = await crud.count_deals(account_id=account_id, pipeline_id=p.id)
        data.append(_details(p, count))
    return PipelineListResponse(data=data, total=len(data), status="success")


async def _provision_default(
    crud: PipelineCrud, account_id: PyObjectId, ctx: RequestContext
) -> Optional[PipelineModel]:
    """Create the seeded default pipeline. Tolerates a concurrent creator."""
    try:
        rows = stage_rules.normalise_stages(defaults.build_default_stages())
        stage_rules.validate_stages(rows)
        pipeline = PipelineModel(
            created_by=account_id,
            name=defaults.DEFAULT_PIPELINE_NAME,
            description="Created automatically when you opened the CRM.",
            is_default=True,
            stages=_dicts_to_stages(rows),
        )
        await crud.insert(pipeline)
        _logger.info(
            f"CRM: provisioned default pipeline for account {account_id}"
        )
        return pipeline
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        # Another request may have won the race — return whatever now exists.
        existing = await crud.list(account_id=account_id)
        return existing[0] if existing else None


@router.get("/{pipeline_id}", response_model=ResponseSchema[PipelineDetails])
async def get_pipeline(
    pipeline_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("pipelines:read")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")
    count = await crud.count_deals(account_id=account_id, pipeline_id=pipeline.id)
    return _ok(_details(pipeline, count))


# ── CRM-025 · create / update / delete ───────────────────────────────────────
@router.post("", response_model=ResponseSchema[PipelineDetails])
async def create_pipeline(
    payload: PipelineCreateRequest,
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Create a pipeline. Omitting ``stages`` seeds a copy of the default set."""
    account_id = _account_id(ctx)

    existing = await crud.list(account_id=account_id, include_archived=True)
    if any((p.name or "").strip().casefold() == payload.name.strip().casefold()
           for p in existing):
        return _fail(f"You already have a pipeline called “{payload.name}”.")

    try:
        if payload.stages:
            # Caller supplies OPEN stages only — Won/Lost come from the defaults,
            # so a client cannot create a pipeline with two Won stages or none.
            rows = stage_rules.assemble_pipeline_stages(
                [
                    {
                        "id": PyObjectId(),
                        "name": s.name,
                        "win_probability": s.win_probability,
                        "color": s.color,
                        "required_attributes": s.required_attributes,
                        "rotting_days": s.rotting_days,
                    }
                    for s in payload.stages
                ],
                defaults.build_default_stages(),
            )
        else:
            rows = stage_rules.normalise_stages(defaults.build_default_stages())
        stage_rules.validate_stages(rows)
    except StageRuleError as ex:
        return _fail(str(ex))

    # First pipeline in an account is always the default.
    make_default = bool(payload.is_default) or not existing
    if make_default:
        await crud.clear_default(account_id=account_id)

    pipeline = PipelineModel(
        created_by=account_id,
        name=payload.name.strip(),
        description=payload.description,
        is_default=make_default,
        weighted_forecast_enabled=bool(payload.weighted_forecast_enabled),
        currency=payload.currency,
        stages=_dicts_to_stages(rows),
    )
    await crud.insert(pipeline)
    return _ok(_details(pipeline, 0), "Pipeline created.")


@router.patch("/{pipeline_id}", response_model=ResponseSchema[PipelineDetails])
async def update_pipeline(
    payload: PipelineUpdateRequest,
    pipeline_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Rename, re-describe, toggle default / weighted forecast / archived.

    Stages are NOT touched here — they have their own endpoints so every
    structural change goes through the invariant checks.
    """
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")

    if payload.name is not None:
        name = payload.name.strip()
        others = await crud.list(account_id=account_id, include_archived=True)
        if any(str(p.id) != str(pipeline.id)
               and (p.name or "").strip().casefold() == name.casefold()
               for p in others):
            return _fail(f"You already have a pipeline called “{name}”.")
        pipeline.name = name

    if payload.description is not None:
        pipeline.description = payload.description
    if payload.weighted_forecast_enabled is not None:
        pipeline.weighted_forecast_enabled = bool(payload.weighted_forecast_enabled)
    if payload.currency is not None:
        pipeline.currency = payload.currency

    if payload.is_default is True:
        await crud.clear_default(account_id=account_id, except_id=pipeline.id)
        pipeline.is_default = True
    elif payload.is_default is False and pipeline.is_default:
        # Never leave an account with zero defaults.
        return _fail("Set another pipeline as the default instead of unsetting this one.")

    if payload.is_archived is not None:
        if payload.is_archived and pipeline.is_default:
            return _fail("Set another pipeline as the default before archiving this one.")
        pipeline.is_archived = bool(payload.is_archived)

    pipeline.updated_at = _utcnow()
    await pipeline.save()
    count = await crud.count_deals(account_id=account_id, pipeline_id=pipeline.id)
    return _ok(_details(pipeline, count), "Pipeline updated.")


@router.delete("/{pipeline_id}", response_model=ResponseSchema)
async def delete_pipeline(
    pipeline_id: PyObjectId = Path(...),
    migrate_to_pipeline_id: Optional[PyObjectId] = Query(None),
    migrate_to_stage_id: Optional[PyObjectId] = Query(None),
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Delete a pipeline, refusing while it holds deals.

    Divergence from Brevo, which deletes a pipeline's deals irreversibly behind
    a single confirm dialog. Pass both ``migrate_to_*`` params to move the
    deals first.
    """
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")

    deal_count = await crud.count_deals(account_id=account_id, pipeline_id=pipeline.id)
    try:
        stage_rules.can_delete_pipeline(
            is_default=bool(pipeline.is_default),
            deal_count=deal_count,
            migrate_to_pipeline_id=migrate_to_pipeline_id,
        )
    except StageRuleError as ex:
        return _fail(str(ex))

    if deal_count and migrate_to_pipeline_id:
        target = await _load(crud, account_id, migrate_to_pipeline_id)
        if not target:
            return _fail("The destination pipeline does not exist.")
        target_stage_ids = {str(s.id) for s in (target.stages or [])}
        if not migrate_to_stage_id or str(migrate_to_stage_id) not in target_stage_ids:
            return _fail("Choose a stage in the destination pipeline for the leads.")
        moved = await _migrate_deals(
            crud, account_id, pipeline.id, migrate_to_pipeline_id, migrate_to_stage_id
        )
        _logger.info(f"CRM: migrated {moved} leads out of pipeline {pipeline.id}")

    await pipeline.delete()
    return _ok(None, "Pipeline deleted.")


async def _migrate_deals(
    crud: PipelineCrud, account_id: Any, from_pipeline: Any,
    to_pipeline: Any, to_stage: Any,
) -> int:
    """Move every deal to another pipeline/stage before deletion."""
    if crud.db_session is None:
        return 0
    try:
        res = await crud.db_session["deals"].update_many(
            {"created_by": account_id, "pipeline_id": from_pipeline},
            {"$set": {"pipeline_id": to_pipeline, "stage_id": to_stage,
                      "stage_entered_at": _utcnow(), "updated_at": _utcnow()}},
        )
        return int(getattr(res, "modified_count", 0) or 0)
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return 0


# ── CRM-026 · stage add / update / delete ────────────────────────────────────
@router.post("/{pipeline_id}/stages", response_model=ResponseSchema[PipelineDetails])
async def add_stage(
    payload: StageCreateRequest,
    pipeline_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Add an open stage. Always inserted before Won/Lost."""
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")

    rows = _stages_to_dicts(pipeline)
    new_stage = {
        "id": PyObjectId(),
        "name": payload.name.strip(),
        "type": "open",
        "win_probability": payload.win_probability,
        "color": payload.color,
        "required_attributes": payload.required_attributes or [],
        "rotting_days": payload.rotting_days,
        "order": 0,
    }

    try:
        rows = stage_rules.insert_open_stage(rows, new_stage, payload.after_stage_id)
        pipeline = await _apply_stages(pipeline, rows)
    except StageRuleError as ex:
        return _fail(str(ex))
    return _ok(_details(pipeline), "Stage added.")


@router.patch("/{pipeline_id}/stages/{stage_id}",
              response_model=ResponseSchema[PipelineDetails])
async def update_stage(
    payload: StageUpdateRequest,
    pipeline_id: PyObjectId = Path(...),
    stage_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Rename, recolour, set probability / required attributes / rotting days.

    Won and Lost accept ``name`` and ``color`` only — their probabilities are
    fixed at 100 and 0, and their required-attribute rules would never fire
    because entering them goes through the close endpoint.
    """
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")

    try:
        rows = stage_rules.apply_stage_edits(
            _stages_to_dicts(pipeline),
            stage_id,
            {
                "name": payload.name,
                "color": payload.color,
                "win_probability": payload.win_probability,
                "required_attributes": payload.required_attributes,
                "rotting_days": payload.rotting_days,
            },
        )
        pipeline = await _apply_stages(pipeline, rows)
    except StageRuleError as ex:
        return _fail(str(ex))
    return _ok(_details(pipeline), "Stage updated.")


@router.delete("/{pipeline_id}/stages/{stage_id}",
               response_model=ResponseSchema[PipelineDetails])
async def delete_stage(
    pipeline_id: PyObjectId = Path(...),
    stage_id: PyObjectId = Path(...),
    move_deals_to_stage_id: Optional[PyObjectId] = Query(None),
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Delete an open stage, moving any deals it holds to a chosen destination."""
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")

    rows = _stages_to_dicts(pipeline)
    deal_count = await crud.count_deals(
        account_id=account_id, pipeline_id=pipeline.id, stage_id=stage_id
    )
    try:
        stage_rules.can_delete_stage(
            rows, stage_id,
            deal_count=deal_count,
            move_deals_to_stage_id=move_deals_to_stage_id,
        )
    except StageRuleError as ex:
        return _fail(str(ex))

    if deal_count and move_deals_to_stage_id:
        await _move_stage_deals(
            crud, account_id, pipeline.id, stage_id, move_deals_to_stage_id
        )

    remaining = [r for r in rows if str(r.get("id")) != str(stage_id)]
    try:
        pipeline = await _apply_stages(pipeline, remaining)
    except StageRuleError as ex:
        return _fail(str(ex))
    return _ok(_details(pipeline), "Stage deleted.")


async def _move_stage_deals(
    crud: PipelineCrud, account_id: Any, pipeline_id: Any,
    from_stage: Any, to_stage: Any,
) -> int:
    if crud.db_session is None:
        return 0
    try:
        res = await crud.db_session["deals"].update_many(
            {"created_by": account_id, "pipeline_id": pipeline_id, "stage_id": from_stage},
            {"$set": {"stage_id": to_stage, "stage_entered_at": _utcnow(),
                      "updated_at": _utcnow()}},
        )
        return int(getattr(res, "modified_count", 0) or 0)
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return 0


# ── CRM-027 · reorder ────────────────────────────────────────────────────────
@router.put("/{pipeline_id}/stages/reorder",
            response_model=ResponseSchema[PipelineDetails])
async def reorder_stages(
    payload: StageReorderRequest,
    pipeline_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """Apply a new order to the OPEN stages.

    The body must list every open stage exactly once — a partial or duplicated
    list is a client bug and is rejected rather than guessed at. Won and Lost
    are implicit and stay pinned last.
    """
    account_id = _account_id(ctx)
    pipeline = await _load(crud, account_id, pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")

    try:
        rows = stage_rules.reorder_open_stages(
            _stages_to_dicts(pipeline), payload.stage_ids
        )
        pipeline = await _apply_stages(pipeline, rows)
    except StageRuleError as ex:
        return _fail(str(ex))
    return _ok(_details(pipeline), "Stages reordered.")
