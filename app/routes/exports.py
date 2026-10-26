"""CRM export API. CRM-235 / CRM-236.

Mounted at ``/api/v1/crm/exports``::

    GET    ""             recent export jobs
    GET    "/{id}"        poll; returns a signed URL once the file exists

and, on the record routers, ``POST /crm/companies/export`` and
``POST /crm/deals/export`` — an export is taken *of a list*, so it is requested
where that list lives and with the same query parameters the list itself takes.

THE FILTERS ARE THE LIST'S FILTERS, PASSED THROUGH VERBATIM. The export handler
does not interpret them; it hands the same keyword dict to the same
``crud.list`` the screen used. An export that resolved filters independently
would eventually disagree with the screen it was launched from, and the
direction that disagreement goes is "the file contains records the caller could
not see".

POST, NOT GET, DESPITE §7.6. A GET that creates a job is not cacheable or
repeatable, and `record_ids` for a selection of ten thousand records does not
fit in a query string. The contract doc records the deviation.

THE SIGNED URL IS MINTED AT POLL TIME. Never stored: a URL that existed from
the moment the file did would sit in the job document, valid, for as long as
the row lives — and an export object is the account's entire customer database.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query

from app.crud import (
    CompanyCrud, CrmAttributeCrud, CrmExportCrud, CrmSettingsCrud, DealCrud,
)
from app.models.export_run import CrmExportModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.export_run import (
    ExportDetails, ExportListResponse, ExportRequest,
)
from app.services import export_rules as er, file_storage
from app.services.export_worker import ExportRunner

router = APIRouter()
company_router = APIRouter()
deal_router = APIRouter()


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fail(message: str, data: Any = None) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=data)


def _actor_name(ctx: RequestContext) -> Optional[str]:
    user = ctx.user
    name = (f"{getattr(user, 'first_name', '') or ''} "
            f"{getattr(user, 'last_name', '') or ''}").strip()
    return name or getattr(user, "email", None)


async def _timezone_of(settings: CrmSettingsCrud, account_id: Any) -> Optional[str]:
    try:
        row = await settings.get_or_create(account_id=account_id)
        return getattr(row, "timezone", None) if row else None
    except Exception:  # pylint: disable=broad-except
        return None


def _details(row: CrmExportModel, *, with_url: bool = False) -> ExportDetails:
    payload = row.model_dump(by_alias=True)
    payload["is_finished"] = row.status in ("completed", "failed")
    # Expired means: it finished, and the sweep has since removed the object.
    payload["is_expired"] = bool(row.status == "completed" and not row.storage_key)

    if with_url and row.status == "completed" and row.storage_key:
        url = file_storage.signed_url(row.storage_key, row.file_name or "export.csv",
                                      "text/csv; charset=utf-8")
        payload["download_url"] = url
        payload["expires_in"] = file_storage.SIGNED_URL_TTL_SECONDS if url else None
    # Belt and braces: ExportDetails declares no storage_key field, so pydantic
    # would drop it anyway. Kept because the cost is one line and the failure it
    # guards against — somebody adding the field to the schema — publishes the
    # location of a file containing the account's entire customer database.
    payload.pop("storage_key", None)
    return ExportDetails.model_validate(payload)


async def _start(
    *, ctx: RequestContext, entity_type: str, payload: ExportRequest,
    filters: Dict[str, Any], background: BackgroundTasks,
    exports: CrmExportCrud, records_crud, attributes: CrmAttributeCrud,
    settings: CrmSettingsCrud,
) -> ResponseSchema:
    """Shared by the company and deal entry points."""
    account_id = _account_id(ctx)

    # Drop unset filters so the stored summary describes what the user actually
    # narrowed by, rather than listing every parameter the endpoint accepts.
    applied = {k: v for k, v in (filters or {}).items()
               if v not in (None, "", [], False)}
    if payload.record_ids:
        applied["record_ids"] = [str(i) for i in payload.record_ids]

    run = CrmExportModel(
        created_by=account_id,
        entity_type=entity_type,
        status="pending",
        filters=applied,
        filter_summary=er.describe_filters(applied),
        columns=list(payload.columns or []),
        separator=payload.separator,
        created_by_user_id=ctx.user.id,
        created_by_name=_actor_name(ctx),
    )
    created = await exports.insert(run)
    if not created:
        return _fail("Could not start that export.")

    runner = ExportRunner(
        export_id=created.id, account_id=account_id, entity_type=entity_type,
        filters={**applied, "record_ids": payload.record_ids or None},
        columns=payload.columns or [], separator=payload.separator,
        tz_name=await _timezone_of(settings, account_id),
        records_crud=records_crud, exports=exports, attributes=attributes,
    )
    background.add_task(runner.run)
    return ResponseSchema(status="success", message="Export started.",
                          data=_details(created))


# ── companies ────────────────────────────────────────────────────────────────
@company_router.post("/export", response_model=ResponseSchema[ExportDetails])
async def export_companies(
    background: BackgroundTasks,
    payload: ExportRequest = ExportRequest(),
    q: Optional[str] = Query(None),
    owner_id: Optional[PyObjectId] = Query(None),
    industry: Optional[str] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    domain: Optional[str] = Query(None),
    is_archived: Optional[bool] = Query(False),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    ctx: RequestContext = Depends(require("companies:read")),
    exports: CrmExportCrud = Depends(get_crud(CrmExportCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Export companies. Takes the SAME query parameters as ``GET /crm/companies``.

    Same parameters, same handler-side dict, same ``crud.list`` — that is what
    makes "exporting a filtered list exports the filter" true by construction
    rather than by two implementations agreeing.
    """
    return await _start(
        ctx=ctx, entity_type="company", payload=payload,
        filters=dict(q=q, owner_id=owner_id, industry=industry, tag_ids=tag_ids,
                     domain=domain, is_archived=is_archived,
                     created_from=created_from, created_to=created_to),
        background=background, exports=exports, records_crud=companies,
        attributes=attributes, settings=settings)


# ── deals ────────────────────────────────────────────────────────────────────
@deal_router.post("/export", response_model=ResponseSchema[ExportDetails])
async def export_deals(
    background: BackgroundTasks,
    payload: ExportRequest = ExportRequest(),
    q: Optional[str] = Query(None),
    pipeline_id: Optional[str] = Query(None),
    stage_id: Optional[PyObjectId] = Query(None),
    owner_id: Optional[PyObjectId] = Query(None),
    status: Optional[str] = Query(None),
    company_id: Optional[PyObjectId] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    close_from: Optional[datetime] = Query(None),
    close_to: Optional[datetime] = Query(None),
    ctx: RequestContext = Depends(require("deals:read")),
    exports: CrmExportCrud = Depends(get_crud(CrmExportCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Export deals. Takes the SAME query parameters as ``GET /crm/deals``."""
    return await _start(
        ctx=ctx, entity_type="deal", payload=payload,
        filters=dict(q=q, pipeline_id=pipeline_id, stage_id=stage_id,
                     owner_id=owner_id, status=status, company_id=company_id,
                     tag_ids=tag_ids, close_from=close_from, close_to=close_to),
        background=background, exports=exports, records_crud=deals,
        attributes=attributes, settings=settings)


# ── polling ──────────────────────────────────────────────────────────────────
@router.get("", response_model=ExportListResponse)
async def list_exports(
    entity_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    ctx: RequestContext = Depends(require("companies:read")),
    exports: CrmExportCrud = Depends(get_crud(CrmExportCrud)),
):
    """Recent exports. No signed URLs here — one is minted per explicit poll,
    so a list request does not hand out a fistful of live download links."""
    account_id = _account_id(ctx)
    filters = exports.build_filter(account_id=account_id, entity_type=entity_type)
    rows = await exports.list_by(filters, sort=[("created_at", -1)],
                                 skip=skip, limit=limit)
    total = await exports.count_by(filters)
    return ExportListResponse(data=[_details(r) for r in rows], total=total,
                              status="success")


@router.get("/{export_id}", response_model=ResponseSchema[ExportDetails])
async def get_export(
    export_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("companies:read")),
    exports: CrmExportCrud = Depends(get_crud(CrmExportCrud)),
):
    """Poll one export. Mints a short-lived signed URL once the file exists.

    The tenancy check happens BEFORE the URL is minted. A signed URL is a bearer
    token — anyone holding it can read the object without authenticating — so a
    check that runs afterwards is not a check.
    """
    row = await exports.get_scoped(_account_id(ctx), export_id)
    if row is None:
        return _fail("That export no longer exists.")
    if row.status == "completed" and not row.storage_key:
        return ResponseSchema(
            status="success",
            message="This export has expired. Run it again for a fresh copy.",
            data=_details(row))
    return ResponseSchema(status="success", message="",
                          data=_details(row, with_url=True))
