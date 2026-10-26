"""Per-user column layouts. CRM-247 / CRM-248.

Mounted at ``/api/v1/crm/view-preferences``::

    GET    ""                  the caller's layout for one object type
    PUT    ""                  save it
    DELETE ""                  reset to the shipped default

**PERMISSION IS THE OBJECT'S READ PERMISSION, NOT A SETTINGS ONE.** Choosing
which columns *you* see is not an administrative act — it changes nothing for
anybody else. Gating it behind ``settings:write`` would mean a rep who can read
the companies list cannot rearrange their own view of it, which is the exact
inversion of who this feature is for.

**THE RESPONSE IS THE RESOLVED LAYOUT, NOT THE STORED ONE.** ``GET`` returns
what the list should render right now — pruned of attributes deleted since the
layout was saved, with labels resolved through the account's own attribute
definitions, falling back to the shipped default when nothing was ever chosen.
A client that received the raw stored list would have to re-implement all of
that against ``GET /crm/attributes``, and the two implementations would drift.

``available`` rides along in the same response so the picker does not need a
second round trip to know what it may offer.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.crud import CrmAttributeCrud, CrmViewPreferenceCrud
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require_any
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.view_preference import ViewPreferenceUpdateRequest
from app.services import column_rules as cr

router = APIRouter()


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _fail(message: str) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=None)


async def _payload(attributes_crud, account_id, user_prefs, user_id,
                   object_type: str) -> dict:
    """The layout to render, plus everything the picker needs to change it."""
    attributes = await attributes_crud.list(account_id=account_id,
                                            object_type=object_type)
    saved = await user_prefs.get_for(account_id, user_id, object_type)
    stored = saved.columns if saved else None

    return {
        "object_type": object_type,
        "columns": cr.resolve(stored, attributes, object_type),
        "available": cr.available(attributes, object_type),
        # Whether the user has chosen anything at all — drives whether the
        # picker offers "Reset to default". Distinct from the layout HAPPENING
        # to equal the default, which `is_default` answers.
        "is_customised": saved is not None,
        # Per object type: a contact's link column is `full_name`, not `name`.
        "locked_columns": list(cr.locked_for(object_type)),
        "max_columns": cr.MAX_COLUMNS,
        # Columns this object type can show but an export cannot produce, so
        # the export dialog can say what it left out instead of quietly
        # returning a narrower file than the screen implied.
        "export_unavailable": sorted(cr.EXPORT_UNAVAILABLE),
    }


def _object_type(value: Optional[str]) -> str:
    return value if value in cr.OBJECT_TYPES else "company"


# ── read ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=ResponseSchema)
async def get_view_preference(
    object_type: str = Query("company", pattern="^(company|deal|contact)$"),
    ctx: RequestContext = Depends(require_any("companies:read", "deals:read")),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    prefs: CrmViewPreferenceCrud = Depends(get_crud(CrmViewPreferenceCrud)),
):
    """The caller's own layout for one object type.

    Never 404s. A user who has never opened the picker has no row, and the
    right answer for them is the shipped default — not an error the list view
    would have to special-case before it could render anything.
    """
    account_id = _account_id(ctx)
    return _ok(await _payload(attributes, account_id, prefs, ctx.user.id,
                              _object_type(object_type)))


# ── write ────────────────────────────────────────────────────────────────────
@router.put("", response_model=ResponseSchema)
async def save_view_preference(
    payload: ViewPreferenceUpdateRequest,
    ctx: RequestContext = Depends(require_any("companies:read", "deals:read")),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    prefs: CrmViewPreferenceCrud = Depends(get_crud(CrmViewPreferenceCrud)),
):
    """Save a layout for the caller.

    PUT, not PATCH: a column layout is an ordered whole. There is no meaningful
    partial update of "these columns, in this order", and offering one would
    invite a client to send an ordering fragment nobody can merge.

    An unknown column is REFUSED here rather than dropped — see
    ``column_rules``. On the way out the same name is pruned instead, because
    by then it means an admin deleted the attribute and failing the read would
    leave the user with a list view that will not render.
    """
    account_id = _account_id(ctx)
    object_type = _object_type(getattr(payload.object_type, "value",
                                       payload.object_type))

    known = await attributes.list(account_id=account_id, object_type=object_type)
    try:
        columns = cr.validate(payload.columns, known, object_type)
    except cr.ColumnError as ex:
        return _fail(str(ex))

    await prefs.save_for(account_id, ctx.user.id, object_type, columns)
    return _ok(await _payload(attributes, account_id, prefs, ctx.user.id,
                              object_type),
               "Columns saved.")


@router.delete("", response_model=ResponseSchema)
async def reset_view_preference(
    object_type: str = Query("company", pattern="^(company|deal|contact)$"),
    ctx: RequestContext = Depends(require_any("companies:read", "deals:read")),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    prefs: CrmViewPreferenceCrud = Depends(get_crud(CrmViewPreferenceCrud)),
):
    """Back to the shipped default.

    The row is DELETED rather than overwritten with the default list, so "never
    chose" stays distinguishable from "chose exactly today's default". A future
    change to the shipped layout then reaches the first group and leaves the
    second alone.
    """
    account_id = _account_id(ctx)
    resolved_type = _object_type(object_type)
    await prefs.clear_for(account_id, ctx.user.id, resolved_type)
    return _ok(await _payload(attributes, account_id, prefs, ctx.user.id,
                              resolved_type),
               "Columns reset.")
