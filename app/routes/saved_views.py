"""Saved views — named filters for the list screens. E23.

Mounted at ``/api/v1/crm/saved-views``::

    GET    ""        the caller's toolbar: their own views plus shared ones
    POST   ""        save one
    PATCH  "/{id}"   rename, re-filter, share, make default
    DELETE "/{id}"   remove it

**PERMISSION IS THE OBJECT'S READ PERMISSION.** Saving a filter you already ran
is not an administrative act — it stores nothing a colleague can see unless you
share it, and gating it behind a settings permission would mean the people who
live in these lists cannot save their own work. Sharing is covered by the
ownership rule in ``saved_view_rules.may_write``, not by a second permission.

⚠️ **THE LIST NESTS: ``data: {data, total}``.** Three CRM GETs do
(``/crm/attributes``, ``/crm/files``, and this one) and a client cannot tell by
looking — `crm.js` passes `nested: true` for exactly these. If you add a list
endpoint, pick the flat shape.

⚠️ **A STALE VIEW IS STILL LISTED.** A view naming an attribute somebody has
since deleted cannot be run, and `stale_fields` says so. It is NOT hidden and
NOT pruned: hiding it leaves a user unable to delete the thing that is
bothering them, and pruning hands back a filter that runs and means something
different from what they saved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.crud import CrmAttributeCrud, CrmSavedViewCrud
from app.models.saved_view import CrmSavedViewModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require_any
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.saved_view import (
    SavedViewCreateRequest, SavedViewDetails, SavedViewUpdateRequest,
)
from app.services import permissions as perms
from app.services import saved_view_rules as svr

router = APIRouter()

READ = require_any("companies:read", "deals:read")


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _fail(message: str) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=None)


def _actor(ctx: RequestContext) -> dict:
    """The three facts every ownership decision here needs."""
    return {
        "user_id": ctx.user.id,
        "is_account_owner": ctx.is_owner,
        "has_manage_others": ctx.has(perms.MANAGE_OTHERS),
    }


async def _attribute_rows(attributes, account_id, object_type) -> List[Any]:
    return await attributes.list(account_id=account_id, object_type=object_type)


def _details(view, *, ctx: RequestContext, attributes=None) -> SavedViewDetails:
    return SavedViewDetails.model_validate(
        svr.payload(view, attributes=attributes, **_actor(ctx)))


# ── list ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=ResponseSchema)
async def list_saved_views(
    object_type: Optional[str] = Query(
        None, pattern="^(company|deal|contact)$",
        description="Omit for both object types."),
    ctx: RequestContext = Depends(READ),
    crud: CrmSavedViewCrud = Depends(get_crud(CrmSavedViewCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """The caller's toolbar.

    Returns their own views plus the shared ones, shared first then alphabetical
    — `saved_view_rules.ordered`, so a view does not move because a colleague
    saved one. `default_view_id` rides along so the list screen knows what to
    open on without a second pass over the rows.
    """
    account_id = _account_id(ctx)
    rows = await crud.list(account_id=account_id, object_type=object_type)
    mine = svr.ordered(svr.visible_to(rows, ctx.user.id))

    # One attribute read per object type present, not one per view — a toolbar
    # of forty views must not be forty queries (NFR6).
    by_type: dict = {}
    for view in mine:
        kind = str(getattr(view, "object_type", "") or "company")
        if kind not in by_type:
            by_type[kind] = await _attribute_rows(attributes, account_id, kind)

    data = [
        _details(view, ctx=ctx,
                 attributes=by_type.get(str(getattr(view, "object_type", "")
                                            or "company")))
        for view in mine
    ]
    defaults = {
        kind: svr.default_for(mine, kind, ctx.user.id) for kind in by_type
    }
    return _ok({
        "data": data,
        "total": len(data),
        "default_view_ids": {
            kind: (getattr(view, "id", None) if view else None)
            for kind, view in defaults.items()
        },
        "max_views": svr.MAX_VIEWS,
    })


# ── create ───────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[SavedViewDetails])
async def create_saved_view(
    payload: SavedViewCreateRequest,
    ctx: RequestContext = Depends(READ),
    crud: CrmSavedViewCrud = Depends(get_crud(CrmSavedViewCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Save a view.

    The group is compiled with `filter_rules` before anything is stored — the
    SAME compiler the list uses, so a view that saves is a view that runs. A
    second, looser validation here is how a user names a filter, sees it on the
    toolbar, clicks it, and gets an error about a filter they were told was
    fine.
    """
    account_id = _account_id(ctx)

    object_type, error = svr.assert_object_type(payload.object_type)
    if error:
        return _fail(error)

    name, error = svr.clean_name(payload.name)
    if error:
        return _fail(error)

    existing = await crud.list(account_id=account_id)
    error = svr.at_capacity(existing, object_type)
    if error:
        return _fail(error)

    error = svr.name_is_free(name, object_type, views=existing,
                             user_id=ctx.user.id, shared=payload.is_shared)
    if error:
        return _fail(error)

    rows = await _attribute_rows(attributes, account_id, object_type)
    error = svr.validate_group(payload.filter_group, object_type, rows)
    if error:
        return _fail(error)

    sort_by, sort_dir = svr.clean_sort(payload.sort_by, payload.sort_dir)
    view = CrmSavedViewModel(
        created_by=account_id,
        created_by_user_id=ctx.user.id,
        object_type=object_type,
        name=name,
        filter_group=payload.filter_group or {},
        columns=list(payload.columns or []),
        sort_by=sort_by,
        sort_dir=sort_dir,
        is_shared=bool(payload.is_shared),
        is_default=bool(payload.is_default),
    )
    created = await crud.insert(view)
    if not created:
        return _fail("Could not save that view.")

    if created.is_default:
        await crud.clear_defaults(account_id=account_id, user_id=ctx.user.id,
                                  object_type=object_type, keep_id=created.id)

    return ResponseSchema(status="success", message=f"“{name}” saved.",
                          data=_details(created, ctx=ctx, attributes=rows))


# ── update ───────────────────────────────────────────────────────────────────
@router.patch("/{view_id}", response_model=ResponseSchema[SavedViewDetails])
async def update_saved_view(
    payload: SavedViewUpdateRequest,
    view_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(READ),
    crud: CrmSavedViewCrud = Depends(get_crud(CrmSavedViewCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Edit a view.

    ⚠️ `exclude_unset`, NOT a None check. Clearing a view's sort means sending
    `sort_by: null`, which a None check would read as "unchanged" — so
    "remove the sort" would silently do nothing and the user would keep
    clicking it.
    """
    account_id = _account_id(ctx)
    view = await crud.get_scoped(account_id, view_id)
    if view is None:
        return _fail("That view no longer exists.")

    # Another person's PERSONAL view is invisible, so it must read as gone
    # rather than as forbidden — a refusal would confirm it exists.
    if not svr.is_shared(view) and svr.owner_of(view) != str(ctx.user.id):
        return _fail("That view no longer exists.")

    if not svr.may_write(view, **_actor(ctx)):
        return _fail(svr.write_refusal(view))

    changes = payload.model_dump(exclude_unset=True)
    object_type = str(view.object_type or "company")
    existing = await crud.list(account_id=account_id)
    rows = await _attribute_rows(attributes, account_id, object_type)

    if "name" in changes:
        name, error = svr.clean_name(changes["name"])
        if error:
            return _fail(error)
        shared = bool(changes.get("is_shared", view.is_shared))
        error = svr.name_is_free(name, object_type, views=existing,
                                 user_id=ctx.user.id, shared=shared,
                                 exclude_id=view.id)
        if error:
            return _fail(error)
        view.name = name

    if "filter_group" in changes:
        group = changes["filter_group"] or {}
        error = svr.validate_group(group, object_type, rows)
        if error:
            return _fail(error)
        view.filter_group = group

    if "columns" in changes:
        view.columns = list(changes["columns"] or [])

    if "sort_by" in changes or "sort_dir" in changes:
        view.sort_by, view.sort_dir = svr.clean_sort(
            changes.get("sort_by", view.sort_by),
            changes.get("sort_dir", view.sort_dir))

    if "is_shared" in changes:
        # Un-sharing a view somebody else was using is destructive but
        # reversible, and it is the owner's call — `may_write` already said so.
        view.is_shared = bool(changes["is_shared"])

    if "is_default" in changes:
        view.is_default = bool(changes["is_default"])

    view.updated_at = datetime.now(timezone.utc)
    await view.save()

    if view.is_default:
        await crud.clear_defaults(account_id=account_id, user_id=ctx.user.id,
                                  object_type=object_type, keep_id=view.id)

    return _ok(_details(view, ctx=ctx, attributes=rows), "View saved.")


# ── delete ───────────────────────────────────────────────────────────────────
@router.delete("/{view_id}", response_model=ResponseSchema)
async def delete_saved_view(
    view_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(READ),
    crud: CrmSavedViewCrud = Depends(get_crud(CrmSavedViewCrud)),
):
    """Delete a view. A hard delete — a saved filter has no history worth keeping.

    Deleting a SHARED view takes it off colleagues' toolbars, which is why
    `may_write` restricts it to the creator and account owners.
    """
    account_id = _account_id(ctx)
    view = await crud.get_scoped(account_id, view_id)
    if view is None:
        return _fail("That view no longer exists.")
    if not svr.is_shared(view) and svr.owner_of(view) != str(ctx.user.id):
        return _fail("That view no longer exists.")
    if not svr.may_write(view, **_actor(ctx)):
        return _fail(svr.write_refusal(view))

    name = view.name
    ok = await crud.delete_scoped(account_id, view_id)
    if not ok:
        return _fail("Could not delete that view.")
    return _ok({"id": str(view_id)}, f"“{name}” deleted.")
