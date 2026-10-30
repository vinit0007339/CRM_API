"""Deal & Kanban board API. CRM-033 / 034 / 035 / 036 / 037 / 038 / 039.

Two routers, because the board reads as a property of a pipeline even though
every line of its logic is about deals:

``router`` mounted at ``/api/v1/crm/deals``::

    GET    ""                      list view (all pipelines by default)
    POST   ""                      create
    GET    "/{deal_id}"            detail
    PATCH  "/{deal_id}"            update — cannot change stage or pipeline
    DELETE "/{deal_id}"            delete (ownership-checked)
    PATCH  "/{deal_id}/stage"      the drag-and-drop endpoint
    POST   "/{deal_id}/close"      mark won / lost
    POST   "/{deal_id}/reopen"     back to an open stage
    POST   "/{deal_id}/links"      attach companies / contacts
    DELETE "/{deal_id}/links"      detach
    GET    "/{deal_id}/activities" timeline

``board_router`` mounted at ``/api/v1/crm/pipelines``::

    GET "/{pipeline_id}/board"                        one request paints the board
    GET "/{pipeline_id}/board/stages/{stage_id}/deals"  "Load more" for one column

The stage-move endpoint is the one to read carefully. Its contract is that a
rejected move leaves the deal **completely unmodified** — no partial write, no
stray activity — because the client has already moved the card optimistically
and will revert on failure. Anything half-applied here shows up as a card that
snaps back but whose data changed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, Path, Query

from app.config import logger as _logger
from app.crud import (
    CompanyCrud, CrmActivityCrud, CrmAssociationCrud, CrmAttributeCrud,
    CrmContactCrud, CrmSettingsCrud, DealCrud, PipelineCrud,
)
from app.models.deal import DealModel
from app.object_id import PyObjectId
from app import foreign

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.services.bulk_service import BulkRunner
from app.schemas.common import (
    BulkActionRequest, BulkActionResult, ResponseSchema,
)
from app.schemas.deal import (
    BoardResponse, DealCloseRequest, DealCreateRequest, DealDetails,
    DealLinksRequest, DealListResponse, DealListSummary, DealReopenRequest,
    DealStageChangeRequest, DealUpdateRequest, StageChangeRejection,
)
from app.services import (
    associations as assoc_rules, attribute_routing, bulk_rules, contact_scope,
    filter_rules, activity_recorder as ar, attributes as attr_rules, forecast,
    live_counts, permissions as perms, position as pos, serializers as sz,
    stage_rules,
)
from app.services.attributes import AttributeError_ as AttributeRuleError
from app.services.filter_service import catalogue_for, fragment_for
from app.services.stage_rules import StageRuleError

router = APIRouter()
board_router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _fail(message: str, data: Any = None) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=data)


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _actor_name(ctx: RequestContext) -> Optional[str]:
    u = ctx.user
    name = f"{getattr(u,'first_name','') or ''} {getattr(u,'last_name','') or ''}".strip()
    return name or getattr(u, "email", None)


def _stage_dicts(pipeline) -> List[Dict[str, Any]]:
    return [s.model_dump() if hasattr(s, "model_dump") else dict(s)
            for s in (pipeline.stages or [])]


def _find_stage(pipeline, stage_id) -> Optional[Dict[str, Any]]:
    return next((s for s in _stage_dicts(pipeline) if str(s.get("id")) == str(stage_id)),
                None)


def _deal_dict(deal: DealModel) -> Dict[str, Any]:
    """The shape stage_rules expects — model fields plus custom_fields."""
    d = deal.model_dump()
    d.setdefault("custom_fields", deal.custom_fields or {})
    return d


async def _owner_index(crud, rows) -> Dict[str, Any]:
    """Batched owner lookup — one query for a whole page, never one per deal."""
    ids = sz.collect_ids(rows, "owner_id")
    # Through `app/foreign.py`: `users` is the platform's collection, and the
    # one-query-per-page rule and the swallow-and-log both live there.
    users = await foreign.users_by_ids(crud.db_session, ids)
    return sz.index_by_id(users, "_id")


async def _all_stages(pipelines: PipelineCrud, account_id,
                      pipeline_id=None) -> List[Dict[str, Any]]:
    """Every stage the rotting filter has to consider. CRM-411.

    Scoped to one pipeline when the list is, every pipeline when it is not —
    `?pipeline_id=all` is the List view's normal mode, and a deal rotting in
    another pipeline is still rotting.

    Returns plain dicts because `stage_rules.rotting_cutoff` reads
    `rotting_days` and `type` off a mapping, and embedded `PipelineStage`
    objects are not one.
    """
    try:
        if pipeline_id:
            pipeline = await pipelines.get_scoped(account_id, pipeline_id)
            rows = [pipeline] if pipeline else []
        else:
            rows = await pipelines.list(account_id=account_id,
                                        include_archived=True)
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return []

    out: List[Dict[str, Any]] = []
    for pipeline in rows:
        for stage in (getattr(pipeline, "stages", None) or []):
            out.append({
                "id": getattr(stage, "id", None),
                # ⚠️ `.value`, not str(): StageType is a str-Enum and
                # str(StageType.OPEN) is "StageType.OPEN".
                "type": getattr(getattr(stage, "type", None), "value",
                                getattr(stage, "type", None)),
                "rotting_days": getattr(stage, "rotting_days", None),
            })
    return out


async def _pipeline_index(pipelines: PipelineCrud, account_id) -> Dict[str, Any]:
    """Every pipeline in the account, keyed by id.

    ⚠️ WITHOUT THIS THE LIST VIEW HAS NO STAGE. `_deal_payload` fills
    ``stage_name`` (and ``weighted_amount``, ``is_rotting``, ``days_in_stage``)
    only when it is handed the deal's pipeline, because the stage NAME lives on
    the pipeline — a deal stores ``stage_id`` and nothing else. Every other
    handler passes one; `list_deals` did not, so the Stage column it ships by
    default rendered an em-dash for every row.

    The board can pass a single pipeline because it IS one pipeline. The list
    spans all of them by default, so it needs the lot — fetched once per
    request and indexed, never once per deal.
    """
    try:
        rows = await pipelines.list(account_id=account_id, include_archived=True)
    except Exception as ex:  # pylint: disable=broad-except
        # Cosmetic expansion, exactly like the owner index: a deal list without
        # stage names beats a deal list that 500s.
        _logger.exception(ex)
        return {}
    return {str(getattr(p, "id", None)): p for p in (rows or [])}


async def _lost_reason_index(settings, account_id) -> Dict[str, str]:
    """``{reason id: label}`` for rendering a lost deal's reason.

    ⚠️ THE DEAL STORES ONLY THE ID. `close_deal` resolves the label to put it
    on the activity record and then throws it away, so `lost_reason_label` —
    declared on `DealDetails` since E6 — was never populated and every lost
    deal rendered a bare "Lost" chip, whatever reason the user had picked.

    Resolved on READ rather than denormalised onto the deal, because a reason
    can be renamed from the settings screen and `merge_lost_reasons` keeps its
    id precisely so old deals follow the rename. A stored copy would freeze the
    old wording. The same module retires reasons instead of deleting them, so
    this lookup still resolves for a reason nobody can pick any more.
    """
    # `getattr` rather than a bare call: a handler invoked directly (the
    # integration tests do) without this dependency receives FastAPI's unresolved
    # `Depends` sentinel, and `settings.get_or_create` on that is an
    # AttributeError the except below would log as if the database had failed.
    get_or_create = getattr(settings, "get_or_create", None)
    if get_or_create is None:
        return {}
    try:
        conf = await get_or_create(account_id=account_id)
    except Exception as ex:  # pylint: disable=broad-except
        # Cosmetic, like the owner index: a deal without its reason label beats
        # a detail page that 500s.
        _logger.exception(ex)
        return {}
    return {str(r.id): r.label
            for r in (getattr(conf, "lost_reasons", None) or []) if r.id}


async def _deal_payload(deal: DealModel, *, crud: DealCrud, pipeline=None,
                        owner_index: Optional[Dict[str, Any]] = None,
                        reason_index: Optional[Dict[str, str]] = None) -> Dict:
    payload = deal.model_dump(by_alias=True)
    if owner_index is None:
        owner_index = await _owner_index(crud, [deal])
    payload["owner"] = sz.user_ref(owner_index.get(str(deal.owner_id)))
    stage = _find_stage(pipeline, deal.stage_id) if pipeline else None
    if stage:
        payload["stage_name"] = stage.get("name")
        payload["weighted_amount"] = forecast.weighted_amount(_deal_dict(deal), stage)
        payload["is_rotting"] = stage_rules.is_rotting(
            stage, deal.stage_entered_at, _utcnow())
        payload["days_in_stage"] = stage_rules.days_in_stage(
            deal.stage_entered_at, _utcnow())
    if deal.lost_reason_id and reason_index:
        payload["lost_reason_label"] = reason_index.get(str(deal.lost_reason_id))
    if deal.primary_company_id:
        payload["primary_company"] = {"id": deal.primary_company_id,
                                      "name": deal.primary_company_name,
                                      "domain": None}
    return payload


async def _route_attribute_values(
    attributes_crud, account_id, object_type, values
):
    """Split a ``custom_fields`` payload into typed columns and custom fields.

    CRM-208. The logic moved to ``attribute_routing`` when the import worker
    became a third caller — two copies of a validation rule is how records that
    arrive by import end up behaving differently from records that were typed
    in. This wrapper stays so the call sites below read unchanged.
    """
    return await attribute_routing.route_for(
        attributes_crud, account_id, object_type, values)


async def _authorize(ctx: RequestContext, deal: DealModel):
    return perms.authorize_modify(
        has_write_permission=True, actor_user_id=ctx.user.id,
        record_owner_id=deal.owner_id, is_account_owner=ctx.is_owner,
        has_manage_others=ctx.has(perms.MANAGE_OTHERS), resource="deal",
    )


# ── CRM-033 · list ───────────────────────────────────────────────────────────
@router.get("", response_model=DealListResponse)
async def list_deals(
    q: Optional[str] = Query(None),
    pipeline_id: Optional[str] = Query(
        None,
        description='A pipeline id, or the literal "all". Omitting it also means all.',
    ),
    stage_id: Optional[PyObjectId] = Query(None),
    status: Optional[str] = Query(None, pattern="^(open|won|lost)$"),
    owner_id: Optional[PyObjectId] = Query(None),
    company_id: Optional[PyObjectId] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    amount_min: Optional[float] = Query(None, ge=0),
    amount_max: Optional[float] = Query(None, ge=0),
    close_date_from: Optional[datetime] = Query(None),
    close_date_to: Optional[datetime] = Query(None),
    has_overdue_task: Optional[bool] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    filters_json: Optional[str] = Query(
        None, alias="filters",
        description="A JSON filter group from the advanced filter builder "
                    '(E22): {"match": "all", "conditions": [...]}',
    ),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
):
    """The List view. Unlike the board it spans every pipeline by default.

    ``pipeline_id`` is typed as a plain string, not PyObjectId, so the
    documented ``?pipeline_id=all`` works. Typed as PyObjectId it 422s on
    "all", which made the contract's own List-view mode unreachable.
    """
    account_id = _account_id(ctx)
    if pipeline_id and pipeline_id.lower() == "all":
        pipeline_id = None
    elif pipeline_id:
        try:
            pipeline_id = PyObjectId(pipeline_id)
        except Exception:  # pylint: disable=broad-except
            return DealListResponse(
                data=[], total=0, status="failure",
                message="That pipeline id is not valid.",
            )
    # CRM-310: see the note on the companies list — a bad group fails, it does
    # not quietly widen to every deal in the account.
    #
    # CRM-411: `is_rotting` is the one filter whose answer is not on the deal —
    # it reads each stage's `rotting_days`. Fetched ONLY when the filter
    # actually mentions it, so an ordinary list request does not pay for a
    # pipeline read it has no use for.
    context = None
    if filter_rules.references(filters_json, filter_rules.ROTTING_FIELD):
        context = {"stages": await _all_stages(pipelines, account_id,
                                               pipeline_id)}
    extra, error = await fragment_for(attributes, account_id, "deal",
                                      filters_json, context)
    if error:
        return DealListResponse(data=[], total=0, status="failure",
                                message=error)
    filters = dict(q=q, pipeline_id=pipeline_id, stage_id=stage_id, status=status,
                   owner_id=owner_id, company_id=company_id, tag_ids=tag_ids,
                   amount_min=amount_min, amount_max=amount_max,
                   close_date_from=close_date_from, close_date_to=close_date_to,
                   has_overdue_task=has_overdue_task, extra=extra)
    rows = await crud.list(account_id=account_id, sort_by=sort_by, sort_dir=sort_dir,
                           skip=skip, limit=limit, **filters)
    total = await crud.count(account_id=account_id, **filters)
    owners = await _owner_index(crud, rows)
    # ARCHIVED PIPELINES INCLUDED, deliberately: a deal on one still has to
    # render its stage. Excluding them would blank the column for exactly the
    # rows a user is most likely to be auditing.
    by_pipeline = await _pipeline_index(pipelines, account_id)
    # ⚠️ THE Contacts COLUMN IS COUNTED, NOT READ OFF THE DEAL, for the same
    # reason as the companies list: `contact_count` is a rollup kept on link
    # and unlink, and a contact deleted from the marketing audience touches
    # neither it nor the association — so the column kept counting somebody the
    # deal's own Contacts panel no longer shows. Two reads for the whole page.
    live = await live_counts.contact_counts(
        assoc_crud=assoc, contact_crud_factory=CrmContactCrud,
        db_session=crud.db_session, account_id=account_id,
        entity_type="deal", entity_ids=[r.id for r in rows])
    data = []
    for r in rows:
        payload = await _deal_payload(
            r, crud=crud, owner_index=owners,
            pipeline=by_pipeline.get(str(r.pipeline_id)))
        counted = live.get(str(r.id))
        if counted is not None:
            payload["contact_count"] = counted
        data.append(DealDetails.model_validate(payload))

    # CRM-311 (E24): the footer's figures.
    #
    # ⚠️ OVER THE WHOLE FILTERED SET, NOT THE PAGE, and computed from the SAME
    # `filters` dict as `rows` and `total`. Summing the 25 rows on screen and
    # calling it the pipeline value is wrong by however many pages there are,
    # and wrong silently — the number still looks like a number.
    summary = await _list_summary(crud, account_id, by_pipeline, filters)

    return DealListResponse(data=data, total=total, status="success",
                            summary=summary)


async def _list_summary(crud: DealCrud, account_id, by_pipeline: Dict[str, Any],
                        filters: Dict[str, Any]):
    """``DealListSummary`` for the current filter, or ``None``.

    Returns None rather than zeroes when the aggregation produced nothing at
    all AND there were no deals: a footer reading "₹0" over a list the user
    knows has deals in it is worse than no footer. Zeroes from a genuinely
    empty result are fine — `deal_count` tells them apart.
    """
    groups = await crud.list_totals(account_id=account_id, **filters)
    # `_stage_dicts`, not `pipeline["stages"]` — `_pipeline_index` hands back
    # MODEL objects, and the stage `type` is a str-enum that only reads
    # correctly through that helper. (`str()` on one returns
    # "StageType.OPEN", which has bitten this module four times.)
    stages_by_id: Dict[str, Any] = {}
    for pipeline in (by_pipeline or {}).values():
        if pipeline is None:
            continue
        for stage in _stage_dicts(pipeline):
            stages_by_id[str(stage.get("id"))] = stage
    try:
        return DealListSummary.model_validate(
            forecast.list_summary(groups, stages_by_id))
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return None


# ── CRM-310 · filter field catalogue ─────────────────────────────────────────
# Above `GET /{deal_id}` for the same reason as on companies: declaration order
# decides, and a PyObjectId segment would swallow "filter-fields".
@router.get("/filter-fields", response_model=ResponseSchema)
async def deal_filter_fields(
    ctx: RequestContext = Depends(require("deals:read")),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Every field a deal can be filtered on, with its operators."""
    fields = await catalogue_for(attributes, _account_id(ctx), "deal")
    return _ok({"fields": fields, "total": len(fields)})


# ── CRM-034 · create / read / update / delete ────────────────────────────────
@router.post("", response_model=ResponseSchema[DealDetails])
async def create_deal(
    payload: DealCreateRequest,
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Create a deal. Pipeline and stage default to the account's default
    pipeline's first open stage, so the simplest possible payload is a name.
    """
    account_id = _account_id(ctx)
    # Which association type a CRM person is on this account's path.
    # One decision, made by `associations.person_entity`.
    unified = contact_scope.unified(
        await conf_crud.get_or_create(account_id=account_id))

    # Falls back to the first pipeline when none is flagged default, so an
    # account that archived its default can still take a deal. See
    # `PipelineCrud.get_default_or_first`.
    pipeline = (await pipelines.get_scoped(account_id, payload.pipeline_id)
                if payload.pipeline_id
                else await pipelines.get_default_or_first(account_id=account_id))
    if not pipeline:
        return _fail("No pipeline found. Create one before adding leads.")

    stages = _stage_dicts(pipeline)
    if payload.stage_id:
        stage = _find_stage(pipeline, payload.stage_id)
        if not stage:
            return _fail("That stage does not belong to the selected pipeline.")
    else:
        # One definition of "the first open stage", shared with the lead door:
        # ordered by `order`, and a stage with no `type` counts as open.
        stage = stage_rules.first_open_stage(stages)
        if not stage:
            return _fail("That pipeline has no open stage to place the lead in.")

    peers = await crud.column_positions(account_id=account_id, pipeline_id=pipeline.id,
                                        stage_id=stage["id"])
    deal = DealModel(
        created_by=account_id,
        created_by_user_id=ctx.user.id,
        owner_id=payload.owner_id or ctx.user.id,
        name=payload.name.strip(),
        pipeline_id=pipeline.id,
        stage_id=stage["id"],
        status=stage_rules.status_for_stage(stage),
        position=pos.append_position(peers[-1] if peers else None),
        amount=payload.amount,
        # ⚠️ THE PIPELINE DECIDES, AND THE PAYLOAD DOES NOT GET A VOTE. This
        # was `payload.currency or pipeline.currency`, which let an API caller
        # or an import put a USD lead inside an INR pipeline — and every total
        # on that board then added the two together as bare numbers. One
        # currency per pipeline is what makes those sums mean anything.
        # See `docs/currency.md`.
        currency=pipeline.currency,
        expected_close_date=payload.expected_close_date,
        probability=payload.probability,
        description=payload.description,
        source=payload.source or "manual",
        tag_ids=payload.tag_ids or [],
        custom_fields={},
    )

    # CRM-208: system names route to the typed columns, custom names to
    # custom_fields, and everything is coerced and validated on the way.
    typed, custom, error = await _route_attribute_values(
        attributes, account_id, "deal", payload.custom_fields)
    if error:
        return _fail(error)
    for field, value in typed.items():
        if value is not None:
            setattr(deal, field, value)
    deal.custom_fields = custom

    if payload.primary_company_id:
        company = await companies.get_scoped(account_id, payload.primary_company_id)
        if company:
            deal.primary_company_id = company.id
            deal.primary_company_name = company.name
    await crud.insert(deal)

    # ⚠️ THE PRIMARY IS LINKED TOO, NOT JUST DENORMALISED. Setting
    # `primary_company_id` above only writes the pair the board card reads; the
    # ASSOCIATION is what `GET /deals/{id}/companies`, the company's Deals tab
    # and the rollup counters all work from. The form happens to send the same
    # id in `company_ids` as well, so this was invisible from our own UI — but
    # a create carrying only `primary_company_id` produced a deal whose header
    # showed a company and whose Companies list was empty. `link` is
    # idempotent, so the overlap costs nothing.
    linkable = list(payload.company_ids or [])
    if deal.primary_company_id and not any(
            str(c) == str(deal.primary_company_id) for c in linkable):
        linkable.append(deal.primary_company_id)
    for company_id in linkable:
        await assoc.link(account_id=account_id, from_type="company", from_id=company_id,
                         to_type="deal", to_id=deal.id, actor_user_id=ctx.user.id)
    for contact_id in payload.contact_ids or []:
        await assoc.link(account_id=account_id, from_type="deal", from_id=deal.id,
                         to_type="contact", to_id=contact_id, actor_user_id=ctx.user.id)
    for crm_contact_id in getattr(payload, "crm_contact_ids", None) or []:
        await assoc.link(account_id=account_id, from_type="deal", from_id=deal.id,
                         to_type=assoc_rules.person_entity(unified),
                         to_id=crm_contact_id, actor_user_id=ctx.user.id)

    await ar.record(activity.write, ar.created(
        entity_type="deal", name=deal.name, source=deal.source,
        account_id=account_id, entity_id=deal.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    return _ok(DealDetails.model_validate(
        await _deal_payload(deal, crud=crud, pipeline=pipeline)), "Lead created.")


@router.get("/{deal_id}", response_model=ResponseSchema[DealDetails])
async def get_deal(
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    pipeline = await pipelines.get_scoped(account_id, deal.pipeline_id)
    return _ok(DealDetails.model_validate(
        await _deal_payload(
            deal, crud=crud, pipeline=pipeline,
            reason_index=await _lost_reason_index(settings, account_id))))


@router.patch("/{deal_id}", response_model=ResponseSchema[DealDetails])
async def update_deal(
    payload: DealUpdateRequest,
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Partial update.

    Cannot move the deal: ``stage_id`` and ``pipeline_id`` are absent from the
    schema and filtered again by ``DEAL_UPDATABLE``, so stage-rule validation,
    positioning and activity recording can never be bypassed.
    """
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    allowed, message = await _authorize(ctx, deal)
    if not allowed:
        return _fail(message)

    updates = sz.allowed_updates(payload.model_dump(), sz.DEAL_UPDATABLE)

    if "custom_fields" in updates:
        typed, custom, error = await _route_attribute_values(
            attributes, account_id, "deal", updates.pop("custom_fields"))
        if error:
            return _fail(error)
        # A partial update merges into the existing dict: a form that sends one
        # attribute must not clear every other custom value on the record.
        merged = dict(deal.custom_fields or {})
        merged.update(custom)
        updates["custom_fields"] = merged
        for field, value in typed.items():
            updates.setdefault(field, value)

    changes = sz.changed_fields(deal, updates)
    pipeline = await pipelines.get_scoped(account_id, deal.pipeline_id)
    if not changes:
        return _ok(DealDetails.model_validate(
            await _deal_payload(
                deal, crud=crud, pipeline=pipeline,
                reason_index=await _lost_reason_index(settings, account_id))),
            "No changes.")

    before = {k: getattr(deal, k, None) for k in changes}
    for key, value in changes.items():
        setattr(deal, key, value)
    deal.updated_at = _utcnow()
    await deal.save()

    await ar.record(activity.write, ar.updated(
        entity_type="deal", before=before, after=changes,
        account_id=account_id, entity_id=deal.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))
    return _ok(DealDetails.model_validate(
        await _deal_payload(
            deal, crud=crud, pipeline=pipeline,
            reason_index=await _lost_reason_index(settings, account_id))),
        "Lead updated.")


@router.delete("/{deal_id}", response_model=ResponseSchema)
async def delete_deal(
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
):
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    allowed, message = await _authorize(ctx, deal)
    if not allowed:
        return _fail(message)
    await assoc.delete_all_for(account_id=account_id, entity_type="deal",
                               entity_id=deal.id)
    await deal.delete()
    return _ok(None, "Lead deleted.")


# ── CRM-037 · the drag-and-drop endpoint ─────────────────────────────────────
# The response_model must admit BOTH outcomes: the moved deal, or the list of
# fields that blocked the move. Typing it as DealDetails alone made every
# rejection a 500 — see StageChangeRejection.
@router.patch("/{deal_id}/stage",
              response_model=ResponseSchema[Union[DealDetails, StageChangeRejection]])
async def change_stage(
    payload: DealStageChangeRequest,
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Move a deal to another stage, and optionally another pipeline.

    Nothing is written until every check has passed — a rejected move must
    leave the deal byte-for-byte unchanged, because the client has already
    moved the card and will revert.
    """
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    allowed, message = await _authorize(ctx, deal)
    if not allowed:
        return _fail(message)

    target_pipeline_id = payload.pipeline_id or deal.pipeline_id
    if str(target_pipeline_id) != str(deal.pipeline_id) and deal.status != "open":
        return _fail("A closed lead cannot be moved to another pipeline.")

    pipeline = await pipelines.get_scoped(account_id, target_pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")
    target_stage = _find_stage(pipeline, payload.stage_id)
    if not target_stage:
        return _fail("That stage does not belong to the selected pipeline.")

    labels = await attributes.labels_for(account_id=account_id, object_type="deal")
    try:
        stage_rules.validate_transition(
            _deal_dict(deal), target_stage,
            pipeline_stage_ids=[s["id"] for s in _stage_dicts(pipeline)],
            labels=labels,
        )
    except StageRuleError as ex:
        # {internal_name, label} pairs, not bare labels: the client renders an
        # input per field and sends the value back under its name (CRM-210).
        missing = stage_rules.missing_required_pairs(
            _deal_dict(deal), target_stage, labels=labels)
        # The client renders exactly this list — it must never compute its own.
        return _fail(str(ex), {"missing_attributes": missing} if missing else None)

    new_position, renormalised = await _resolve_position(
        crud, account_id, pipeline.id, target_stage["id"], payload)

    previous_stage = _find_stage(pipeline, deal.stage_id) or \
        await _stage_from_other_pipeline(pipelines, account_id, deal)
    days = stage_rules.days_in_stage(deal.stage_entered_at, _utcnow())

    deal.pipeline_id = pipeline.id
    # ⚠️ THE CURRENCY MOVES WITH IT. A move used to set `pipeline_id` and
    # leave `currency` alone, so a USD lead dropped into an INR pipeline kept
    # USD — and that pipeline's column totals silently became a mix with
    # nothing on screen to say so. This is the one line that stops a pipeline
    # going mixed without anybody choosing it.
    #
    # ⚠️ IT DOES NOT CONVERT THE AMOUNT, and must not pretend to: 5,000 is
    # still 5,000. Moving a lead between pipelines of different currencies is
    # a re-denomination the person doing it has to mean — the amount is theirs
    # to restate. Logged so it is visible rather than silent.
    if pipeline.currency and deal.currency != pipeline.currency:
        _logger.info(
            f"CRM: lead {deal.id} moved into pipeline {pipeline.id}; currency "
            f"{deal.currency} -> {pipeline.currency} (amount unchanged)")
        deal.currency = pipeline.currency
    deal.stage_id = target_stage["id"]
    deal.status = stage_rules.status_for_stage(target_stage)
    deal.position = new_position
    deal.stage_entered_at = _utcnow()
    deal.updated_at = _utcnow()
    deal.last_activity_at = _utcnow()
    await deal.save()

    await ar.record(activity.write, ar.stage_changed(
        from_stage=previous_stage, to_stage=target_stage, days_in_previous_stage=days,
        account_id=account_id, entity_id=deal.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    message = "Lead moved."
    if renormalised:
        message = "Lead moved. Card positions in that column were re-spaced."
    return _ok(DealDetails.model_validate(
        await _deal_payload(deal, crud=crud, pipeline=pipeline)), message)


async def _resolve_position(crud, account_id, pipeline_id, stage_id, payload):
    """Fractional position from the dropped card's neighbours (CRM-015)."""
    before = after = None
    if payload.before_deal_id:
        neighbour = await crud.get_scoped(account_id, payload.before_deal_id)
        before = neighbour.position if neighbour else None
    if payload.after_deal_id:
        neighbour = await crud.get_scoped(account_id, payload.after_deal_id)
        after = neighbour.position if neighbour else None

    column = await crud.column_positions(account_id=account_id, pipeline_id=pipeline_id,
                                         stage_id=stage_id)
    if before is None and after is None:
        return pos.append_position(column[-1] if column else None), False
    return pos.resolve(before, after, column_size=len(column))


async def _stage_from_other_pipeline(pipelines, account_id, deal):
    """The deal's previous stage when it lived in a different pipeline."""
    origin = await pipelines.get_scoped(account_id, deal.pipeline_id)
    return _find_stage(origin, deal.stage_id) if origin else None


# ── CRM-038 · close / reopen ─────────────────────────────────────────────────
@router.post("/{deal_id}/close", response_model=ResponseSchema[DealDetails])
async def close_deal(
    payload: DealCloseRequest,
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Mark a deal won or lost.

    Sets status, close date and moves it to the pipeline's Won/Lost stage in a
    single operation — which is why the stage endpoint refuses to do it.
    """
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    allowed, message = await _authorize(ctx, deal)
    if not allowed:
        return _fail(message)

    outcome = getattr(payload.outcome, "value", payload.outcome)
    if outcome not in ("won", "lost"):
        return _fail("A lead can only be closed as won or lost.")
    if deal.status != "open":
        return _fail("That lead is already closed. Reopen it first.")

    pipeline = await pipelines.get_scoped(account_id, deal.pipeline_id)
    if not pipeline:
        return _fail("That pipeline no longer exists.")
    target = next((s for s in _stage_dicts(pipeline) if s.get("type") == outcome), None)
    if not target:
        return _fail(f"That pipeline has no {outcome.title()} stage.")

    reason_label = None
    reason_index: Dict[str, str] = {}
    if outcome == "lost":
        if not payload.lost_reason_id:
            return _fail("Choose a reason for losing this lead.")
        conf = await settings.get_or_create(account_id=account_id)
        reason = next((r for r in (conf.lost_reasons if conf else [])
                       if str(r.id) == str(payload.lost_reason_id)), None)
        if not reason:
            return _fail("That lost reason is not in your list.")
        reason_label = reason.label
        deal.lost_reason_id = payload.lost_reason_id
        # The label the response needs, already in hand. `_lost_reason_index`
        # would re-read the settings document for the one id we just matched.
        reason_index = {str(payload.lost_reason_id): reason_label}

    if payload.amount is not None:
        deal.amount = payload.amount
    deal.actual_close_date = payload.actual_close_date or _utcnow()
    deal.close_note = payload.close_note
    deal.stage_id = target["id"]
    deal.status = outcome
    deal.stage_entered_at = _utcnow()
    deal.updated_at = _utcnow()
    await deal.save()

    await ar.record(activity.write, ar.closed(
        outcome=outcome, amount=deal.amount, close_date=deal.actual_close_date,
        reason=reason_label, note=payload.close_note,
        account_id=account_id, entity_id=deal.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    return _ok(DealDetails.model_validate(
        await _deal_payload(deal, crud=crud, pipeline=pipeline,
                            reason_index=reason_index)),
        f"Lead marked {outcome}.")


@router.post("/{deal_id}/reopen", response_model=ResponseSchema[DealDetails])
async def reopen_deal(
    payload: DealReopenRequest,
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Reopen a closed deal into a chosen open stage, clearing the close fields."""
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    allowed, message = await _authorize(ctx, deal)
    if not allowed:
        return _fail(message)
    if deal.status == "open":
        return _fail("That lead is already open.")

    pipeline = await pipelines.get_scoped(account_id, deal.pipeline_id)
    target = _find_stage(pipeline, payload.stage_id) if pipeline else None
    if not target:
        return _fail("That stage does not belong to the selected pipeline.")
    if target.get("type") != "open":
        return _fail("Choose an open stage to reopen the lead into.")

    column = await crud.column_positions(account_id=account_id, pipeline_id=pipeline.id,
                                         stage_id=target["id"])
    deal.stage_id = target["id"]
    deal.status = "open"
    deal.position = pos.append_position(column[-1] if column else None)
    deal.actual_close_date = None
    deal.lost_reason_id = None
    deal.close_note = None
    deal.stage_entered_at = _utcnow()
    deal.updated_at = _utcnow()
    await deal.save()

    await ar.record(activity.write, ar.reopened(
        to_stage=target, account_id=account_id, entity_id=deal.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))
    return _ok(DealDetails.model_validate(
        await _deal_payload(deal, crud=crud, pipeline=pipeline)), "Lead reopened.")


# ── CRM-039 · links & activities ─────────────────────────────────────────────
@router.post("/{deal_id}/links", response_model=ResponseSchema)
async def add_deal_links(
    payload: DealLinksRequest,
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Attach companies/contacts, and optionally set the primary company.

    The primary company is what the Kanban card shows, so setting it also
    refreshes the denormalised name on the deal.
    """
    account_id = _account_id(ctx)
    # Which association type a CRM person is on this account's path.
    # One decision, made by `associations.person_entity`.
    unified = contact_scope.unified(
        await conf_crud.get_or_create(account_id=account_id))
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")

    changed = 0
    already = 0
    for company_id in payload.company_ids or []:
        # ⚠️ SCOPED FIRST. `assoc.link` normalises the pair but does not check
        # that the company is this account's, so an id from another tenant used
        # to produce an association row, a 200 and "1 record(s) linked" — and
        # then nothing on the page, because every read of the deal's companies
        # filters on `created_by`. A link to a record the account cannot see is
        # a link nobody can ever use.
        company = await companies.get_scoped(account_id, company_id)
        if not company:
            return _fail("That company no longer exists.")
        if await assoc.link(account_id=account_id, from_type="company",
                            from_id=company.id, to_type="deal", to_id=deal.id,
                            actor_user_id=ctx.user.id):
            changed += 1
        else:
            already += 1
    for contact_id in payload.contact_ids or []:
        if await assoc.link(account_id=account_id, from_type="deal", from_id=deal.id,
                            to_type="contact", to_id=contact_id,
                            actor_user_id=ctx.user.id):
            changed += 1
        else:
            already += 1
    for crm_contact_id in payload.crm_contact_ids or []:
        if await assoc.link(account_id=account_id, from_type="deal", from_id=deal.id,
                            to_type=assoc_rules.person_entity(unified),
                            to_id=crm_contact_id,
                            actor_user_id=ctx.user.id):
            changed += 1
        else:
            already += 1

    if payload.primary_company_id:
        company = await companies.get_scoped(account_id, payload.primary_company_id)
        if not company:
            return _fail("That company no longer exists.")
        if await assoc.link(account_id=account_id, from_type="company",
                            from_id=company.id, to_type="deal", to_id=deal.id,
                            is_primary=True, actor_user_id=ctx.user.id):
            changed += 1
        # The deal's own `primary_company_id` is what every read uses — the
        # association's `is_primary` flag is written but nothing reads it, so
        # this call, not the flag, is what makes a company primary.
        await assoc.set_primary_company(account_id=account_id, deal_id=deal.id,
                                        company_id=company.id, company_name=company.name)
    # ⚠️ "0 record(s) linked." WAS REPORTED AS A SUCCESS WITH NO EXPLANATION.
    # Attaching a company that is already attached is not a failure, but
    # telling the user it succeeded while the count says nothing happened is
    # how a working screen looks broken.
    if not changed and already:
        return _ok({"changed": 0, "already_linked": already},
                   "Already linked." if already == 1
                   else f"All {already} were already linked.")
    return _ok({"changed": changed, "already_linked": already},
               f"{changed} record(s) linked.")


@router.delete("/{deal_id}/links", response_model=ResponseSchema)
async def remove_deal_links(
    payload: DealLinksRequest,
    deal_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    account_id = _account_id(ctx)
    # Which association type a CRM person is on this account's path.
    # One decision, made by `associations.person_entity`.
    unified = contact_scope.unified(
        await conf_crud.get_or_create(account_id=account_id))
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")
    changed = 0
    for company_id in payload.company_ids or []:
        if await assoc.unlink(account_id=account_id, from_type="company",
                              from_id=company_id, to_type="deal", to_id=deal.id):
            changed += 1
    for contact_id in payload.contact_ids or []:
        if await assoc.unlink(account_id=account_id, from_type="deal", from_id=deal.id,
                              to_type="contact", to_id=contact_id):
            changed += 1
    for crm_contact_id in payload.crm_contact_ids or []:
        if await assoc.unlink(account_id=account_id, from_type="deal", from_id=deal.id,
                              to_type=assoc_rules.person_entity(unified),
                              to_id=crm_contact_id):
            changed += 1

    # ⚠️ DETACHING THE PRIMARY MUST CLEAR THE DENORMALISED FIELDS. They are
    # what the board card, the list column and the deal header all read, so
    # leaving them behind shows a company the deal is no longer linked to —
    # and clicking it opens a record with no sign of this deal.
    detached = {str(c) for c in (payload.company_ids or [])}
    if deal.primary_company_id and str(deal.primary_company_id) in detached:
        deal.primary_company_id = None
        deal.primary_company_name = None
        deal.updated_at = _utcnow()
        await deal.save()

    return _ok({"changed": changed}, f"{changed} record(s) unlinked.")


@router.get("/{deal_id}/contacts", response_model=ResponseSchema)
async def deal_contacts(
    deal_id: PyObjectId = Path(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
):
    """The people linked to this deal — platform contacts and CRM-owned both.

    ⚠️ WITHOUT THIS A DEAL'S PEOPLE WERE A NUMBER. `POST /crm/deals` has always
    accepted `contact_ids` and written the association, `contact_count` is
    maintained on the deal, and the detail page renders
    `["Contacts", deal.contact_count]` — so a deal could say "Contacts: 3" with
    no way to find out who they were. The write path existed and nothing read
    it back, the same shape of gap `GET /{deal_id}/companies` was added to fix.

    ⚠️ BOTH ASSOCIATION TYPES, IN ONE RESPONSE, AND THE ROW SAYS WHICH IS
    WHICH. `to_type="contact"` is a person in the platform's marketing audience,
    read through `app/foreign.py`; `to_type="crm_contact"` is this service's
    own. `app/routes/companies.py` resolves them side by side for the same
    reason and its comments record why: the backfill that would have merged
    them was abandoned, so nothing has to guess which collection an id belongs
    to — the association names it.

    Each row carries `source` so a caller can tell them apart without
    inferring it from which fields are populated.

    ⚠️ AND THIS READS BOTH TYPES UNCONDITIONALLY, WHICH IS WHY IT NEEDED NO
    FLAG IN PHASE 6. On the unified path new links are written as `contact`,
    so they arrive in the first set and the second is empty; pre-cutover links
    are still `crm_contact` and arrive in the second. Both render. That makes
    this the one endpoint that is correct *during* the phase 7 migration as
    well as on either side of it — a deal half-migrated shows all its people,
    not half of them.
    """
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")

    platform_ids = await assoc.counterparts(
        account_id=account_id, entity_type="deal", entity_id=deal_id,
        want_type="contact")
    crm_ids = await assoc.counterparts(
        account_id=account_id, entity_type="deal", entity_id=deal_id,
        want_type="crm_contact")

    total = len(platform_ids) + len(crm_ids)
    # ⚠️ PAGED ACROSS THE TWO SETS AS ONE SEQUENCE, not two pages stitched
    # together. Platform ids come first and keep their order, so a `skip` that
    # lands inside the CRM-owned half does not silently re-serve the platform
    # half's first row.
    ordered = [("contact", i) for i in platform_ids] + \
              [("crm_contact", i) for i in crm_ids]
    page = ordered[skip:skip + limit] if limit else []

    want_platform = [i for kind, i in page if kind == "contact"]
    want_crm = [i for kind, i in page if kind == "crm_contact"]

    def _ref(row: Any, source: str) -> Dict[str, Any]:
        """One person, with ids as strings.

        ⚠️ `sz.contact_ref` RETURNS THE RAW ObjectId, and that is fine
        everywhere else because every other caller feeds it into a declared
        schema (`CompanyDetails`) which coerces it. This endpoint answers a
        plain dict under `response_model=ResponseSchema`, so an ObjectId
        reaches pydantic's serialiser untyped and raises
        `PydanticSerializationError` — a 500 on a read, from a value that is
        correct. Stringified here rather than by changing `contact_ref`, whose
        other callers rely on the current behaviour.
        """
        ref = sz.contact_ref(row)
        if ref.get("id") is not None:
            ref["id"] = str(ref["id"])
        ref["source"] = source
        return ref

    rows: List[Dict[str, Any]] = []
    if want_platform:
        # ⚠️ NOT ACCOUNT-SCOPED, and `app/foreign.py` explains why: this is an
        # EXPANSION of ids that already came off this account's own association
        # rows, so they are already its own.
        for row in await foreign.contacts_by_ids(crud.db_session, want_platform):
            rows.append(_ref(row, "platform"))
    if want_crm:
        from app.crud.contact import CrmContactCrud

        contacts = CrmContactCrud(crud.db_session)
        for row in await contacts.list_by(
                {"created_by": contacts.oid(account_id),
                 "_id": {"$in": list(want_crm)}}, limit=len(want_crm)):
            rows.append(_ref(row, "crm"))

    # ⚠️ `total` COUNTS ASSOCIATIONS; `data` COUNTS PEOPLE WE COULD RESOLVE,
    # and the two can differ. A platform contact deleted on the marketing side
    # leaves its association row behind, so the deal still says it has a person
    # and the row cannot be rendered. `GET /{company_id}/contacts` has the same
    # property and returns `total: len(ids)` for the same reason: hiding the
    # discrepancy would turn a visible "one of these is gone" into a silently
    # shorter list.
    # ⚠️ `total` COUNTS PEOPLE WE RESOLVED, NOT ASSOCIATION ROWS — AND IT USED
    # TO COUNT THE ROWS. Deleting a contact from the audience leaves its
    # association behind (nothing on the platform side knows this service's
    # collections exist), so a record whose only person had been deleted still
    # reported "Contacts (1)" with an empty list under it.
    #
    # ⚠️ ONLY WHEN THIS REQUEST COVERED EVERY ASSOCIATION. On a later page, or
    # a capped one, the rows in hand are a window and `len(rows)` would be the
    # size of the window, not of the set — so the association count stands and
    # the caller's "could not be loaded" note still explains the difference.
    # `limit=0` asks for no rows at all and is never "everything".
    #
    # ⚠️ THE ROWS ARE NOT DELETED HERE. `contacts_by_ids` returns `[]` for a
    # lookup that FAILED as readily as for one that found nothing (it swallows
    # the exception by design — see `host/directory.py`), so a dangling row and
    # an unreachable host are indistinguishable from here. Counting is
    # reversible; pruning on that signal would not be.
    resolved_all = skip == 0 and bool(limit) and len(ordered) <= limit
    return _ok({"data": rows, "total": len(rows) if resolved_all else total,
                "counts": {"platform": len(platform_ids), "crm": len(crm_ids)}})


@router.get("/{deal_id}/companies", response_model=ResponseSchema)
async def deal_companies(
    deal_id: PyObjectId = Path(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
):
    """The companies linked to this deal. The mirror of
    ``GET /crm/companies/{id}/deals``, which has existed since E5.

    ⚠️ WITHOUT THIS THERE WAS NO WAY TO SEE A DEAL'S COMPANIES. The deal's
    detail response carries only `primary_company` — the denormalised pair the
    board card shows — so attaching a second company succeeded, answered 200,
    wrote the association, and changed nothing the user could see. The screen
    looked broken while the data was right.

    The primary is identified by the DEAL's `primary_company_id`, not by the
    association's `is_primary` flag: the flag is written but nothing reads it,
    and two sources for one fact is how they come to disagree.
    """
    account_id = _account_id(ctx)
    deal = await crud.get_scoped(account_id, deal_id)
    if not deal:
        return _fail("That lead no longer exists.")

    ids = await assoc.counterparts(account_id=account_id, entity_type="deal",
                                   entity_id=deal_id, want_type="company")
    page = ids[skip:skip + limit] if limit else []
    rows = await companies.list_by(
        {"created_by": account_id, "_id": {"$in": page}}, limit=limit
    ) if page else []

    primary = str(deal.primary_company_id) if deal.primary_company_id else None
    data = [{"id": c.id, "name": c.name, "domain": c.domain,
             "industry": c.industry, "owner_id": c.owner_id,
             "is_primary": str(c.id) == primary} for c in rows]
    # The primary leads, then alphabetical — a list whose order changes with
    # whichever row the driver returned first reads as random.
    data.sort(key=lambda c: (not c["is_primary"], (c["name"] or "").lower()))
    # ⚠️ `json_safe`, OR THIS ANSWERS 500. `c.id` and `c.owner_id` are raw
    # `bson.ObjectId`s, and this route declares a bare `ResponseSchema`, so
    # `data` is untyped: pydantic meets an ObjectId with no schema for it and
    # raises PydanticSerializationError. The integration shims' ObjectId
    # subclasses `str`, so the tests passed with this missing — the same trap
    # `serializers.json_safe` was written for, and the reason the assertions
    # below check `type(...) is str` rather than `isinstance`.
    return _ok({"data": sz.json_safe(data), "total": len(ids)})


@router.get("/{deal_id}/activities", response_model=ResponseSchema)
async def deal_activities(
    deal_id: PyObjectId = Path(...),
    activity_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(30, ge=0, le=100),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)
    if not await crud.get_scoped(account_id, deal_id):
        return _fail("That lead no longer exists.")
    rows = await activity.list(account_id=account_id, entity_type="deal",
                               entity_id=deal_id, activity_type=activity_type,
                               skip=skip, limit=limit)
    total = await activity.count(account_id=account_id, entity_type="deal",
                                 entity_id=deal_id, activity_type=activity_type)
    return _ok({"data": [sz.public_dump(r) for r in rows], "total": total})


# ── CRM-035 / 036 · the board ────────────────────────────────────────────────
@board_router.get("/{pipeline_id}/board", response_model=BoardResponse)
async def get_board(
    pipeline_id: PyObjectId = Path(...),
    q: Optional[str] = Query(None),
    owner_id: Optional[PyObjectId] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    close_date_from: Optional[datetime] = Query(None),
    close_date_to: Optional[datetime] = Query(None),
    has_overdue_task: Optional[bool] = Query(None),
    cards_per_stage: int = Query(25, ge=1, le=100),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """One request paints the whole board.

    Counts and sums honour the same filters as the cards, so a column header
    can never disagree with what is visible beneath it.
    """
    account_id = _account_id(ctx)
    pipeline = await pipelines.get_scoped(account_id, pipeline_id)
    if not pipeline:
        return BoardResponse(status="failure", message="That pipeline no longer exists.")

    filters = dict(q=q, owner_id=owner_id, tag_ids=tag_ids,
                   close_date_from=close_date_from, close_date_to=close_date_to,
                   has_overdue_task=has_overdue_task)
    summary = await crud.board_summary(account_id=account_id, pipeline=pipeline,
                                       cards_per_stage=cards_per_stage, **filters)

    now = _utcnow()
    all_cards = [d for e in summary["stages"] for d in e["deals"]]
    owners = await _owner_index(crud, all_cards)
    stages = []
    for entry in summary["stages"]:
        stage = entry["stage"]
        cards = []
        for deal in entry["deals"]:
            card = deal.model_dump(by_alias=True)
            card["owner"] = sz.user_ref(owners.get(str(deal.owner_id)))
            card["is_rotting"] = stage_rules.is_rotting(stage, deal.stage_entered_at, now)
            # Per-card contribution to the column header. Uses the same
            # function the list and detail responses use, so the three can
            # never quietly disagree.
            card["weighted_amount"] = forecast.weighted_amount(_deal_dict(deal), stage)
            if deal.primary_company_id:
                card["primary_company"] = {"id": deal.primary_company_id,
                                           "name": deal.primary_company_name,
                                           "domain": None}
            cards.append(card)
        stages.append({
            "id": stage["id"], "name": stage["name"], "order": stage["order"],
            "type": stage.get("type", "open"),
            "win_probability": stage.get("win_probability", 0),
            "color": stage.get("color"),
            "required_attributes": stage.get("required_attributes") or [],
            "deal_count": entry["deal_count"], "total_value": entry["total_value"],
            "weighted_value": entry["weighted_value"], "has_more": entry["has_more"],
            "deals": cards,
        })

    return BoardResponse(status="success", data={
        "pipeline": {"id": pipeline.id, "name": pipeline.name,
                     "currency": pipeline.currency,
                     "weighted_forecast_enabled": pipeline.weighted_forecast_enabled},
        "totals": summary["totals"],
        "stages": stages,
    })


@board_router.get("/{pipeline_id}/board/stages/{stage_id}/deals",
                  response_model=DealListResponse)
async def board_stage_deals(
    pipeline_id: PyObjectId = Path(...),
    stage_id: PyObjectId = Path(...),
    q: Optional[str] = Query(None),
    owner_id: Optional[PyObjectId] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    close_date_from: Optional[datetime] = Query(None),
    close_date_to: Optional[datetime] = Query(None),
    has_overdue_task: Optional[bool] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    ctx: RequestContext = Depends(require("deals:read")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
):
    """"Load more" for one column. Brevo hard-caps its board at 1,000 deals;
    we page instead.

    THE FILTER SET HERE MUST MATCH ``get_board``'s. It previously accepted only
    ``q`` and ``owner_id``, so pressing "Load more" on a filtered board appended
    cards the filter excluded and reported a ``total`` computed without them —
    the column silently stopped meaning what its header said. If you add a
    filter to the board, add it here in the same commit.
    """
    account_id = _account_id(ctx)
    pipeline = await pipelines.get_scoped(account_id, pipeline_id)
    if not pipeline:
        return DealListResponse(data=[], total=0, status="failure")
    filters = dict(q=q, owner_id=owner_id, tag_ids=tag_ids,
                   close_date_from=close_date_from, close_date_to=close_date_to,
                   has_overdue_task=has_overdue_task)
    # One filter dict for the page and the count, so they cannot drift.
    rows = await crud.stage_cards(account_id=account_id, pipeline_id=pipeline_id,
                                  stage_id=stage_id, skip=skip, limit=limit, **filters)
    total = await crud.count(account_id=account_id, pipeline_id=pipeline_id,
                             stage_id=stage_id, **filters)
    owners = await _owner_index(crud, rows)
    data = [DealDetails.model_validate(
        await _deal_payload(r, crud=crud, pipeline=pipeline, owner_index=owners))
        for r in rows]
    return DealListResponse(data=data, total=total, status="success")


@router.post("/bulk", response_model=ResponseSchema[BulkActionResult])
async def bulk_deals(
    payload: BulkActionRequest,
    hard: bool = Query(False, description="Permanently delete (owner/admin only)"),
    ctx: RequestContext = Depends(require("deals:write")),
    crud: DealCrud = Depends(get_crud(DealCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Apply one action to many deals. CRM-204.

    Takes EITHER ``ids`` OR ``filters`` — never both, never neither. The filter
    form is what powers "select all 2,847 matching" without the client
    enumerating ids, and it resolves through the same ``crud.list`` the list
    endpoint uses, so the set acted on is the set that was on screen.

    A PARTIAL RESULT IS A SUCCESS. HTTP 200 with the counts: a bulk operation
    that half-worked did half-work, and returning a failure would tell the
    client to retry the whole thing. ``updated``, ``skipped``,
    ``skipped_no_permission`` and ``failed`` are separate because they are
    different problems — one needs a permission change, one needs a different
    selection, one needs us to fix something.

    The cap is REPORTED, not silently applied: a request matching 9,000 records
    that changes 1,000 says so, because a silent truncation is
    indistinguishable from success.
    """
    account_id = _account_id(ctx)
    if hard and not ctx.is_owner and not ctx.has(perms.MANAGE_OTHERS):
        return _fail("Only an account owner can permanently delete records.")

    runner = BulkRunner(
        entity_type="deal", account_id=account_id, crud=crud,
        activity=activity, actor_user_id=ctx.user.id,
        actor_name=_actor_name(ctx), is_account_owner=ctx.is_owner,
        has_manage_others=ctx.has(perms.MANAGE_OTHERS),
    )
    # CRM-310: same set as the list, advanced filter included. See the note on
    # `bulk_companies` — the fragment is compiled here, not sent by the client.
    extra, error = await fragment_for(attributes, account_id, "deal",
                                      payload.filter_group)
    if error:
        return _fail(error)

    try:
        result = await runner.run(action=payload.action, payload=payload.payload,
                                  ids=payload.ids, filters=payload.filters,
                                  hard=hard, extra=extra)
    except bulk_rules.BulkError as ex:
        return _fail(str(ex))

    notice = result.pop("cap_notice", None)
    message = result.pop("message", "")
    if notice:
        message = f"{message}. {notice}"
    return _ok(BulkActionResult.model_validate(result), message)
