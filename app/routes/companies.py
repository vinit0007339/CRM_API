"""Company API. CRM-028 / 029 / 030 / 031 / 032.

Mounted at ``/api/v1/crm/companies``::

    GET    ""                          list (search, filter, sort, paginate)
    POST   ""                          create (warns on duplicates, never blocks)
    GET    "/duplicates"               suggested duplicate pairs
    GET    "/{company_id}"             detail (one request paints the page)
    PATCH  "/{company_id}"             update  — never touches associations
    DELETE "/{company_id}"             archive, or ?hard=true to remove
    POST   "/{company_id}/links"       attach contacts / deals   (additive)
    DELETE "/{company_id}/links"       detach contacts / deals   (subtractive)
    GET    "/{company_id}/contacts"    linked contacts
    GET    "/{company_id}/deals"       linked deals
    GET    "/{company_id}/activities"  timeline

Design notes worth knowing before editing:

* **Associations are never settable through PATCH.** Brevo's PATCH replaces the
  linked-id array wholesale, silently unlinking anything absent from the
  payload; they needed a second endpoint to work around it. Ours is enforced
  twice — the schema has no such field, and ``allowed_updates`` filters again.
* **Duplicate domains warn, they don't block** (CRM plan §3.3): subsidiaries,
  franchises and regional entities legitimately share a parent domain.
* **No N+1** (NFR6): every handler collects related ids in one pass, fetches
  each related collection once, and hands the index to ``serializers.py``.
* Business failures return **HTTP 200** with ``status="failure"`` — house
  convention, so the UI renders the message inline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.config import logger as _logger
from app.crud import (
    CompanyCrud, CrmActivityCrud, CrmAssociationCrud, CrmAttributeCrud, DealCrud,
)
from app.models.company import CompanyModel
from app.object_id import PyObjectId
from app import foreign

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.services.bulk_service import BulkRunner
from app.schemas.common import (
    BulkActionRequest, BulkActionResult, ResponseSchema,
)
from app.schemas.company import (
    CompanyCreateRequest, CompanyDetails, CompanyLinksRequest,
    CompanyListResponse, CompanyMergeRequest, CompanyUpdateRequest,
)
from app.services import (
    attribute_routing, bulk_rules,
    activity_recorder as ar, attributes as attr_rules, dedupe,
    merge_rules, permissions as perms, serializers as sz,
)
from app.services.filter_service import catalogue_for, fragment_for
from app.services.merge_service import MergeService

router = APIRouter()

LINKED_PREVIEW = 20   # linked records embedded in the detail response


# ── helpers ──────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


async def _route_attribute_values(
    attributes_crud, account_id, values
):
    """Split a ``custom_fields`` payload into typed columns and custom fields.

    CRM-208. The logic moved to ``attribute_routing`` when the import worker
    became a third caller — two copies of a validation rule is how records that
    arrive by import end up behaving differently from records that were typed
    in. This wrapper stays so the call sites below read unchanged.
    """
    return await attribute_routing.route_for(
        attributes_crud, account_id, "company", values)


def _fail(message: str) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=None)


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


async def _lookup_indexes(crud: CompanyCrud, rows: List[Any]) -> tuple:
    """One batched query per related collection — never one per row (NFR6)."""
    owner_ids = sz.collect_ids(rows, "owner_id")
    tag_ids = sz.collect_ids(rows, "tag_ids")
    # ⚠️ THROUGH `app/foreign.py`. `users` and `tags` belong to the platform,
    # and every read of them in this service goes through that one module — so
    # that splitting the databases later is a change to one file rather than a
    # search through 38,000 lines. The swallow-and-log is in there too:
    # expansion is cosmetic and must never fail the list.
    owners = await foreign.users_by_ids(crud.db_session, owner_ids)
    tags = await foreign.tags_by_ids(crud.db_session, tag_ids)
    return sz.index_by_id(owners, "_id"), sz.index_by_id(tags, "_id")


def _record(activity_crud: CrmActivityCrud, activity: Optional[Dict[str, Any]]) -> Any:
    return ar.record(activity_crud.write, activity)


# ── CRM-028 · list ───────────────────────────────────────────────────────────
@router.get("", response_model=CompanyListResponse)
async def list_companies(
    q: Optional[str] = Query(None, description="Search name and domain"),
    owner_id: Optional[PyObjectId] = Query(None),
    industry: Optional[str] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    domain: Optional[str] = Query(None),
    is_archived: Optional[bool] = Query(False),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    filters_json: Optional[str] = Query(
        None, alias="filters",
        description="A JSON filter group from the advanced filter builder "
                    '(E22): {"match": "all", "conditions": [...]}',
    ),
    ctx: RequestContext = Depends(require("companies:read")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """List companies. Read is account-wide — ownership never narrows it."""
    account_id = _account_id(ctx)
    # CRM-310: the advanced filter. A BAD GROUP IS A FAILURE, NOT AN EMPTY
    # `extra` — answering an unfiltered list would show every company under a
    # filter chip the user can see, which is the one outcome worse than an
    # error.
    extra, error = await fragment_for(attributes, account_id, "company",
                                      filters_json)
    if error:
        return CompanyListResponse(data=[], total=0, status="failure",
                                   message=error)
    filters = dict(
        q=q, owner_id=owner_id, industry=industry, tag_ids=tag_ids, domain=domain,
        is_archived=is_archived, created_from=created_from, created_to=created_to,
        extra=extra,
    )
    rows = await crud.list(account_id=account_id, sort_by=sort_by, sort_dir=sort_dir,
                           skip=skip, limit=limit, **filters)
    # Same builder, same arguments — the count can't disagree with the page.
    total = await crud.count(account_id=account_id, **filters)

    owner_index, tag_index = await _lookup_indexes(crud, rows)
    data = [
        CompanyDetails.model_validate(
            sz.company_payload(r, owner_index=owner_index, tag_index=tag_index)
        )
        for r in rows
    ]
    return CompanyListResponse(data=data, total=total, status="success")


# ── CRM-310 · filter field catalogue ─────────────────────────────────────────
# Declared ABOVE `GET /{company_id}`: FastAPI matches in declaration order, and
# a PyObjectId path parameter would capture "filter-fields" and 422 on it.
@router.get("/filter-fields", response_model=ResponseSchema)
async def company_filter_fields(
    ctx: RequestContext = Depends(require("companies:read")),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Every field a company can be filtered on, with its operators.

    Gated on `companies:read`, not on a settings permission: this describes
    what you may already look at, and the builder is unusable without it.
    """
    fields = await catalogue_for(attributes, _account_id(ctx), "company")
    return _ok({"fields": fields, "total": len(fields)})


# ── CRM-029 · create ─────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[CompanyDetails])
async def create_company(
    payload: CompanyCreateRequest,
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Create a company.

    A duplicate domain produces ``data.duplicate_warning`` alongside the created
    record — it never rejects. The UI offers "Open existing" or "Create anyway".
    """
    account_id = _account_id(ctx)
    normalised_domain = dedupe.normalize_domain(payload.domain)

    duplicates = await crud.find_duplicates(
        account_id=account_id, name=payload.name, domain=normalised_domain
    )
    warning = sz.pick_duplicate_warning(duplicates)

    company = CompanyModel(
        created_by=account_id,
        created_by_user_id=ctx.user.id,
        owner_id=payload.owner_id or ctx.user.id,
        **dedupe.derive_name_fields(payload.name),
        domain=normalised_domain,
        website=str(payload.website) if payload.website else None,
        linkedin_url=str(payload.linkedin_url) if payload.linkedin_url else None,
        industry=payload.industry,
        employee_count=payload.employee_count,
        annual_revenue=payload.annual_revenue,
        currency=payload.currency,
        phone=payload.phone,
        phone_country_code=payload.phone_country_code,
        address=payload.address.model_dump() if payload.address else None,
        description=payload.description,
        tag_ids=payload.tag_ids or [],
        custom_fields={},
        source="manual",
    )

    # CRM-208: system names route to the typed columns, custom names to
    # custom_fields, everything coerced and validated on the way through.
    typed, custom, error = await _route_attribute_values(
        attributes, account_id, payload.custom_fields)
    if error:
        return _fail(error)
    for field, value in typed.items():
        if value is not None:
            setattr(company, field, value)
    company.custom_fields = custom

    await crud.insert(company)

    # Convenience: create-time associations. On UPDATE these are not accepted.
    for contact_id in payload.contact_ids or []:
        await assoc.link(account_id=account_id, from_type="company", from_id=company.id,
                         to_type="contact", to_id=contact_id, actor_user_id=ctx.user.id)
    for deal_id in payload.deal_ids or []:
        await assoc.link(account_id=account_id, from_type="company", from_id=company.id,
                         to_type="deal", to_id=deal_id, actor_user_id=ctx.user.id)

    await _record(activity, ar.created(
        entity_type="company", name=company.name, source="manual",
        account_id=account_id, entity_id=company.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx),
    ))

    owner_index, tag_index = await _lookup_indexes(crud, [company])
    data = CompanyDetails.model_validate(sz.company_payload(
        company, owner_index=owner_index, tag_index=tag_index,
        duplicate_warning=warning,
    ))
    message = "Company created."
    if warning:
        message = f"Company created. “{warning['name']}” may be a duplicate."
    return _ok(data, message)


def _actor_name(ctx: RequestContext) -> Optional[str]:
    user = ctx.user
    name = f"{getattr(user,'first_name','') or ''} {getattr(user,'last_name','') or ''}".strip()
    return name or getattr(user, "email", None)


# ── CRM-032 · detail ─────────────────────────────────────────────────────────
@router.get("/duplicates", response_model=ResponseSchema)
async def list_duplicates(
    min_confidence: str = Query("medium", pattern="^(high|medium|low)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    scan_limit: int = Query(1000, ge=1, le=5000,
                            description="How many companies to compare"),
    ctx: RequestContext = Depends(require("companies:read")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
):
    """Suggested duplicate pairs across the account. CRM-238.

    Declared before ``/{company_id}`` so the literal path wins the match.

    CONFIDENCE TIERS, NEVER A BOOLEAN. An exact domain match and a fuzzy name
    resemblance are not the same claim; collapsing them into "duplicate" either
    buries the certain ones or invites merging the uncertain ones. ``low`` is a
    similarity suggestion and is noisy enough to sit behind an explicit choice,
    so the default is ``medium``.

    Each pair appears ONCE. Asking every company "who matches you?" naturally
    yields A-B and B-A, and a review screen listing both makes the account look
    twice as messy as it is.

    Both sides carry the SAME fields, including the counts and the age: which of
    two records to keep is decided on how old each is and how much hangs off it,
    and a reviewer who has to open both in new tabs will not use this.
    """
    account_id = _account_id(ctx)
    companies = await crud.list_by(
        {"created_by": account_id, "is_archived": False},
        sort=[("created_at", -1)], limit=scan_limit)
    by_id = {str(c.id): c for c in companies}

    seen, pairs = set(), []
    for company in companies:
        matches = await crud.find_duplicates(
            account_id=account_id, name=company.name, domain=company.domain,
            exclude_id=company.id, min_confidence=min_confidence,
        )
        for match in matches:
            key = tuple(sorted([str(company.id), str(match["id"])]))
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "left": _duplicate_side(company),
                "right": _duplicate_side(by_id.get(str(match["id"])), match),
                "confidence": match["confidence"],
                "matched_on": match["matched_on"],
            })

    rank = {dedupe.HIGH: 0, dedupe.MEDIUM: 1, dedupe.LOW: 2}
    pairs.sort(key=lambda p: rank.get(p["confidence"], 3))
    return _ok({"data": sz.json_safe(pairs[skip:skip + limit]),
                "total": len(pairs)})


def _duplicate_side(company, match=None):
    """One side of a candidate pair, with what a reviewer needs to choose.

    Both sides go through here so they have the same shape. The previous
    version returned a company_ref on the left and a bare dict on the right,
    which made the UI read two different objects for one comparison.
    """
    if company is None:
        return {"id": (match or {}).get("id"), "name": (match or {}).get("name"),
                "domain": (match or {}).get("domain")}
    return {
        "id": company.id,
        "name": company.name,
        "domain": company.domain,
        "industry": company.industry,
        "owner_id": company.owner_id,
        "deal_count": company.deal_count or 0,
        "contact_count": company.contact_count or 0,
        "created_at": company.created_at,
        "last_activity_at": company.last_activity_at,
    }


@router.get("/{company_id}", response_model=ResponseSchema[CompanyDetails])
async def get_company(
    company_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("companies:read")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
):
    """One request paints the whole detail page — no follow-up call for Overview."""
    account_id = _account_id(ctx)
    company = await crud.get_scoped(account_id, company_id)
    if not company:
        return _fail("That company no longer exists.")

    contact_ids = await assoc.counterparts(
        account_id=account_id, entity_type="company", entity_id=company.id,
        want_type="contact")
    deal_ids = await assoc.counterparts(
        account_id=account_id, entity_type="company", entity_id=company.id,
        want_type="deal")

    contacts = await _fetch_contacts(crud, contact_ids[:LINKED_PREVIEW])
    deal_rows = await deals.list_by(
        {"created_by": account_id, "_id": {"$in": deal_ids[:LINKED_PREVIEW]}},
        limit=LINKED_PREVIEW,
    ) if deal_ids else []

    owner_index, tag_index = await _lookup_indexes(crud, [company])
    data = CompanyDetails.model_validate(sz.company_payload(
        company, owner_index=owner_index, tag_index=tag_index,
        linked_contacts=sz.linked_collection(contacts, len(contact_ids), sz.contact_ref),
        linked_deals=sz.linked_collection(
            deal_rows, len(deal_ids),
            lambda d: {"id": d.id, "name": d.name, "amount": d.amount,
                       "status": getattr(d.status, "value", d.status),
                       "stage_id": d.stage_id,
                       "expected_close_date": d.expected_close_date},
        ),
    ))
    return _ok(data)


async def _fetch_contacts(crud: CompanyCrud, contact_ids: List[Any]) -> List[Any]:
    """A company's Contacts tab. The rows are PLATFORM contacts — see
    `app/foreign.py`, including why this particular read is not re-scoped."""
    return await foreign.contacts_by_ids(crud.db_session, contact_ids)


# ── CRM-029 · update / delete ────────────────────────────────────────────────
@router.patch("/{company_id}", response_model=ResponseSchema[CompanyDetails])
async def update_company(
    payload: CompanyUpdateRequest,
    company_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Partial update. Never modifies associations — use the links endpoints."""
    account_id = _account_id(ctx)
    company = await crud.get_scoped(account_id, company_id)
    if not company:
        return _fail("That company no longer exists.")

    allowed, message = perms.authorize_modify(
        has_write_permission=True, actor_user_id=ctx.user.id,
        record_owner_id=company.owner_id, is_account_owner=ctx.is_owner,
        has_manage_others=ctx.has(perms.MANAGE_OTHERS), resource="company",
    )
    if not allowed:
        return _fail(message)

    updates = sz.allowed_updates(payload.model_dump(), sz.COMPANY_UPDATABLE)

    if "custom_fields" in updates:
        typed, custom, error = await _route_attribute_values(
            attributes, account_id, updates.pop("custom_fields"))
        if error:
            return _fail(error)
        # Merge, don't replace: a form sending one attribute must not clear
        # every other custom value on the record.
        merged = dict(company.custom_fields or {})
        merged.update(custom)
        updates["custom_fields"] = merged
        for field, value in typed.items():
            updates.setdefault(field, value)
    if "domain" in updates:
        updates["domain"] = dedupe.normalize_domain(updates["domain"])
    changes = sz.changed_fields(company, updates)
    if not changes:
        owner_index, tag_index = await _lookup_indexes(crud, [company])
        return _ok(CompanyDetails.model_validate(sz.company_payload(
            company, owner_index=owner_index, tag_index=tag_index)), "No changes.")

    before = {k: getattr(company, k, None) for k in changes}
    renamed = "name" in changes
    if renamed:
        # Through the one writer, so the normalised form cannot fall behind the
        # name — a company renamed to something that should match a duplicate
        # would otherwise stop matching it, silently.
        changes.update(dedupe.derive_name_fields(changes["name"]))
    for key, value in changes.items():
        setattr(company, key, value)
    company.updated_at = _utcnow()
    await company.save()

    if renamed:
        # Keep the denormalised name on this company's deal cards in step.
        await crud.rename_fanout(account_id=account_id, company_id=company.id,
                                 new_name=company.name)

    await _record(activity, ar.updated(
        entity_type="company", before=before, after=changes,
        account_id=account_id, entity_id=company.id,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx),
    ))

    owner_index, tag_index = await _lookup_indexes(crud, [company])
    return _ok(CompanyDetails.model_validate(sz.company_payload(
        company, owner_index=owner_index, tag_index=tag_index)), "Company updated.")


@router.delete("/{company_id}", response_model=ResponseSchema)
async def delete_company(
    company_id: PyObjectId = Path(...),
    hard: bool = Query(False, description="Permanently delete (owner/admin only)"),
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Archive by default; ``?hard=true`` removes the record and its links.

    A hard delete never cascades to contacts or deals — only the association
    rows go, and their counters are decremented.
    """
    account_id = _account_id(ctx)
    company = await crud.get_scoped(account_id, company_id)
    if not company:
        return _fail("That company no longer exists.")

    allowed, message = perms.authorize_modify(
        has_write_permission=True, actor_user_id=ctx.user.id,
        record_owner_id=company.owner_id, is_account_owner=ctx.is_owner,
        has_manage_others=ctx.has(perms.MANAGE_OTHERS), resource="company",
    )
    if not allowed:
        return _fail(message)

    if hard and not ctx.is_owner and not ctx.has(perms.MANAGE_OTHERS):
        return _fail("Only an account owner can permanently delete a company.")

    if hard:
        await assoc.delete_all_for(account_id=account_id, entity_type="company",
                                   entity_id=company.id)
        await company.delete()
        return _ok(None, "Company deleted.")

    company.is_archived = True
    company.updated_at = _utcnow()
    await company.save()
    await _record(activity, ar.build_activity(
        account_id=account_id, entity_type="company", entity_id=company.id,
        activity_type="updated", title="Company archived",
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx),
    ))
    return _ok(None, "Company archived.")


# ── CRM-030 · links ──────────────────────────────────────────────────────────
@router.post("/{company_id}/links", response_model=ResponseSchema)
async def add_links(
    payload: CompanyLinksRequest,
    company_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Attach contacts and/or deals. Additive and idempotent."""
    return await _mutate_links(payload, company_id, ctx, crud, assoc, activity, attach=True)


@router.delete("/{company_id}/links", response_model=ResponseSchema)
async def remove_links(
    payload: CompanyLinksRequest,
    company_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Detach contacts and/or deals. Subtractive — never a replace."""
    return await _mutate_links(payload, company_id, ctx, crud, assoc, activity, attach=False)


async def _mutate_links(payload, company_id, ctx, crud, assoc, activity, *, attach: bool):
    account_id = _account_id(ctx)
    company = await crud.get_scoped(account_id, company_id)
    if not company:
        return _fail("That company no longer exists.")

    op = assoc.link if attach else assoc.unlink
    changed = 0
    for contact_id in payload.contact_ids or []:
        kwargs = dict(account_id=account_id, from_type="company", from_id=company.id,
                      to_type="contact", to_id=contact_id)
        if attach:
            kwargs["actor_user_id"] = ctx.user.id
        if await op(**kwargs):
            changed += 1
    for deal_id in payload.deal_ids or []:
        kwargs = dict(account_id=account_id, from_type="company", from_id=company.id,
                      to_type="deal", to_id=deal_id)
        if attach:
            kwargs["actor_user_id"] = ctx.user.id
        if await op(**kwargs):
            changed += 1

    if changed:
        # Writes to BOTH records' timelines — the counterpart entry is written
        # by the association service.
        await _record(activity, ar.linked(
            entity_type="company", other_type="contact" if payload.contact_ids else "deal",
            other_name=None, other_id=None, unlink=not attach,
            account_id=account_id, entity_id=company.id,
            actor_user_id=ctx.user.id, actor_name=_actor_name(ctx),
        ))
    verb = "linked" if attach else "unlinked"
    return _ok({"changed": changed}, f"{changed} record(s) {verb}.")


# ── CRM-031 · sub-resources ──────────────────────────────────────────────────
@router.get("/{company_id}/contacts", response_model=ResponseSchema)
async def company_contacts(
    company_id: PyObjectId = Path(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    ctx: RequestContext = Depends(require("companies:read")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
):
    account_id = _account_id(ctx)
    if not await crud.get_scoped(account_id, company_id):
        return _fail("That company no longer exists.")
    ids = await assoc.counterparts(account_id=account_id, entity_type="company",
                                   entity_id=company_id, want_type="contact")
    page = ids[skip:skip + limit] if limit else []
    rows = await _fetch_contacts(crud, page)
    return _ok({"data": [sz.contact_ref(r) for r in rows], "total": len(ids)})


@router.get("/{company_id}/deals", response_model=ResponseSchema)
async def company_deals(
    company_id: PyObjectId = Path(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    ctx: RequestContext = Depends(require("companies:read")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
):
    account_id = _account_id(ctx)
    if not await crud.get_scoped(account_id, company_id):
        return _fail("That company no longer exists.")
    ids = await assoc.counterparts(account_id=account_id, entity_type="company",
                                   entity_id=company_id, want_type="deal")
    page = ids[skip:skip + limit] if limit else []
    rows = await deals.list_by(
        {"created_by": account_id, "_id": {"$in": page}}, limit=limit
    ) if page else []
    # ⚠️ `json_safe`, OR THIS ANSWERS 500 — the same defect as in
    # `GET /deals/{id}/companies`, sitting here since E5. `d.id`, `d.stage_id`
    # and `d.owner_id` are raw `bson.ObjectId`s and this route declares a bare
    # `ResponseSchema`, so `data` is untyped and pydantic has no schema for
    # them. Unnoticed because no screen calls this endpoint yet.
    return _ok({
        "data": sz.json_safe(
            [{"id": d.id, "name": d.name, "amount": d.amount,
              "status": getattr(d.status, "value", d.status),
              "stage_id": d.stage_id, "owner_id": d.owner_id,
              "expected_close_date": d.expected_close_date} for d in rows]),
        "total": len(ids),
    })


@router.get("/{company_id}/activities", response_model=ResponseSchema)
async def company_activities(
    company_id: PyObjectId = Path(...),
    activity_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(30, ge=0, le=100),
    ctx: RequestContext = Depends(require("companies:read")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)
    if not await crud.get_scoped(account_id, company_id):
        return _fail("That company no longer exists.")
    rows = await activity.list(
        account_id=account_id, entity_type="company", entity_id=company_id,
        activity_type=activity_type, skip=skip, limit=limit,
    )
    total = await activity.count(
        account_id=account_id, entity_type="company", entity_id=company_id,
        activity_type=activity_type,
    )
    return _ok({
        "data": [sz.public_dump(r) for r in rows],
        "total": total,
    })


@router.post("/merge", response_model=ResponseSchema)
async def merge_companies(
    payload: CompanyMergeRequest,
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Merge ``merged_id`` into ``survivor_id``. CRM-239.

    Both ids are resolved WITHIN the account before anything happens: a merge
    takes an id from the client and archives the record it names, which is the
    last place to trust one.
    """
    account_id = _account_id(ctx)
    survivor = await crud.get_scoped(account_id, payload.survivor_id)
    loser = await crud.get_scoped(account_id, payload.merged_id)
    if survivor is None or loser is None:
        return _fail("One of those companies no longer exists.")

    # The ownership rule of an edit, applied to BOTH records: a merge edits the
    # survivor and removes the loser, so being allowed to touch one of them is
    # not enough.
    for record in (survivor, loser):
        allowed, message = perms.authorize_modify(
            has_write_permission=True, actor_user_id=ctx.user.id,
            record_owner_id=record.owner_id, is_account_owner=ctx.is_owner,
            has_manage_others=ctx.has(perms.MANAGE_OTHERS), resource="company",
        )
        if not allowed:
            return _fail(message)

    service = MergeService(db=crud.db_session, account_id=account_id,
                           companies=crud, actor_user_id=ctx.user.id,
                           actor_name=_actor_name(ctx))
    loser_name = loser.name
    try:
        snapshot = await service.merge(survivor, loser,
                                       payload.field_resolution or {})
    except merge_rules.MergeError as ex:
        return _fail(str(ex))

    await _record(activity, ar.build_activity(
        account_id=account_id, entity_type="company", entity_id=survivor.id,
        activity_type="merged",
        title=merge_rules.summarise(loser_name, snapshot.get("moved")),
        metadata=sz.json_safe({
            "merged_id": loser.id, "merged_name": loser_name,
            "field_resolution": payload.field_resolution or {},
            "moved": {k: len(v or []) for k, v in (snapshot.get("moved") or {}).items()},
        }),
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    owner_index, tag_index = await _lookup_indexes(crud, [survivor])
    return _ok({
        # Stated, not implied by argument order: "which one survived?" is the
        # question people get wrong about a merge.
        "survivor": sz.company_payload(survivor, owner_index=owner_index,
                                       tag_index=tag_index),
        "merged_id": str(loser.id),
        "merged_name": loser_name,
        "reversible_until": merge_rules.expiry(loser.merged_at),
        "moved": {k: len(v or []) for k, v in (snapshot.get("moved") or {}).items()},
    }, f"{loser_name} merged into {survivor.name}.")


@router.post("/{company_id}/unmerge", response_model=ResponseSchema)
async def unmerge_company(
    company_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Undo a merge, within 30 days. CRM-240.

    ``company_id`` is the record that was merged AWAY — the one that
    disappeared. That is the one the user is looking for when they want it
    back.
    """
    account_id = _account_id(ctx)
    loser = await crud.get_scoped(account_id, company_id)
    if loser is None:
        return _fail("That company no longer exists.")

    service = MergeService(db=crud.db_session, account_id=account_id,
                           companies=crud, actor_user_id=ctx.user.id,
                           actor_name=_actor_name(ctx))
    survivor_id = loser.merged_into_id
    try:
        result = await service.unmerge(loser)
    except merge_rules.MergeError as ex:
        return _fail(str(ex))

    await _record(activity, ar.build_activity(
        account_id=account_id, entity_type="company", entity_id=survivor_id,
        activity_type="unmerged", title=f"Merge with {loser.name} undone",
        metadata=sz.json_safe({"restored_id": loser.id,
                               "restored_name": loser.name}),
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    owner_index, tag_index = await _lookup_indexes(crud, [loser])
    return _ok({
        "restored": sz.company_payload(loser, owner_index=owner_index,
                                       tag_index=tag_index),
        "survivor_id": result.get("survivor_id"),
    }, f"{loser.name} restored.")


@router.post("/bulk", response_model=ResponseSchema[BulkActionResult])
async def bulk_companies(
    payload: BulkActionRequest,
    hard: bool = Query(False, description="Permanently delete (owner/admin only)"),
    ctx: RequestContext = Depends(require("companies:write")),
    crud: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Apply one action to many companies. CRM-204.

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
        entity_type="company", account_id=account_id, crud=crud,
        activity=activity, actor_user_id=ctx.user.id,
        actor_name=_actor_name(ctx), is_account_owner=ctx.is_owner,
        has_manage_others=ctx.has(perms.MANAGE_OTHERS),
    )
    # CRM-310: "select all matching" has to mean the SAME set the list showed,
    # advanced filter included — and the fragment is compiled here, from the
    # group, never accepted from the client.
    extra, error = await fragment_for(attributes, account_id, "company",
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
