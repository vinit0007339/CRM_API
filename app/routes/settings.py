"""CRM account settings API. CRM-019.

Mounted at ``/api/v1/crm/settings``::

    GET "" the account's CRM configuration

WHY THIS EXISTS AS ITS OWN ROUTE, and why it is read-only for now.

``POST /crm/deals/{id}/close`` refuses a lost deal without a
``lost_reason_id``, and validates that id against ``crm_settings.lost_reasons``.
Until this endpoint existed there was no way for a client to discover that
list, so the Close-as-lost dialog had nothing to populate its picker with and
the flow was unreachable from the UI. Read access is therefore a Phase 1
requirement, not a settings-screen nicety.

WRITING is deliberately NARROW: ``PATCH ""`` accepts only the fields that have
a screen and tests behind them — the auto-link pair (CRM-243) and the lost
reasons. Industries and task types are still read-only, because a PATCH that
accepts a field with no UI and no tests is how a settings API grows fields
nobody validates.

⚠️ A LOST REASON IS NEVER DELETED. ``close_deal`` stores only the id, so a
dropped row takes the label of every deal closed against it. A reason left out
of the PATCH is retired and kept — see ``lost_reason_rules``, which enforces
that so it cannot depend on the client getting it right.

PERMISSION: ``deals:read``. Everything this returns is either needed to work a
deal or is harmless picklist metadata; gating it behind ``pipelines:write``
would lock the Close dialog for exactly the reps who use it most.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from app.config import logger as _logger
from app.crud import CrmAttributeCrud, CrmSettingsCrud
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.settings import CrmSettingsUpdateRequest
from app.services import attributes as attr_rules
from app.services import lost_reason_rules as lr
from app.services import serializers as sz
from app.services.attributes import AttributeError_

router = APIRouter()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read a stored lost reason that may be a model OR a raw dict.

    Beanie normally hands back ``LostReason`` objects, but a document that
    predates a field, a lazily-parsed read, or a raw ``update_one`` written
    outside the model all produce plain dicts. ``r.id`` on one of those is an
    AttributeError, and an AttributeError here is a 500.
    """
    if isinstance(row, dict):
        value = row.get(name, default)
    else:
        value = getattr(row, name, default)
    return default if value is None else value


def _lost_reason_rows(conf: Any) -> List[Dict[str, Any]]:
    """Serialise the picklist, tolerating anything already in the collection.

    ⚠️ A SETTINGS READ MUST NOT 500. This endpoint feeds the Close-as-lost
    dialog AND the settings screen that edits the picklist, so one malformed
    row taking the response down means nobody can close a deal as lost and
    nobody can open the screen that would repair the row — the failure removes
    its own fix.

    ``order`` is the specific hazard. It is typed ``int`` now, but a row stored
    before that, or one written by a raw update, can hold ``null``, and
    ``sorted()`` raises ``TypeError: '<' not supported between instances of
    'NoneType' and 'int'``. Undated rows sort first rather than blowing up.
    """
    rows: List[Dict[str, Any]] = []
    for row in (getattr(conf, "lost_reasons", None) or []):
        try:
            order = _field(row, "order", 0)
            rows.append({
                "id": _field(row, "id"),
                "label": str(_field(row, "label", "")),
                "order": order if isinstance(order, int) else 0,
                "is_active": bool(_field(row, "is_active", True)),
            })
        except Exception as ex:  # pylint: disable=broad-except
            # One bad row is not worth the whole picklist.
            _logger.warning(f"crm_settings: unreadable lost reason: {ex}")

    rows.sort(key=lambda r: r["order"])
    return rows


def _default_lost_reasons() -> List[str]:
    """The shipped labels, or nothing. Never the reason a settings read fails."""
    try:
        return [r["label"] for r in lr.default_rows()]
    except Exception as ex:  # pylint: disable=broad-except
        _logger.warning(f"crm_settings: default lost reasons: {ex}")
        return []


@router.get("", response_model=ResponseSchema)
async def get_settings(
    ctx: RequestContext = Depends(require("deals:read")),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """The account's CRM configuration, creating defaults on first read.

    Retired lost reasons are returned WITH their ``is_active`` flag rather than
    filtered out: a historical deal still needs its label to render, so the
    client filters for pickers and looks up by id for display.
    """
    account_id = _account_id(ctx)
    conf = await settings.get_or_create(account_id=account_id)
    if not conf:
        return ResponseSchema(
            status="failure", message="Could not load your CRM settings.", data=None
        )

    try:
        # ⚠️ json_safe IS NOT OPTIONAL HERE. This route declares a bare
        # `ResponseSchema`, so `data` is an untyped container: pydantic has no
        # schema for what is inside it, meets the raw `bson.ObjectId` behind
        # each lost reason's `id`, and raises
        #     Unable to serialize unknown type: <class 'bson.objectid.ObjectId'>
        # -> HTTP 500. `PyObjectId` only serialises itself when a field is
        # TYPED as one, which nothing here is.
        #
        # This was latent for as long as `lost_reasons` was empty on every
        # account. The moment settings started auto-provisioning with the seven
        # default reasons, every id in this response became an ObjectId and the
        # endpoint every CRM screen loads first began 500ing.
        return _ok(sz.json_safe({
            "currency": conf.currency,
            # The shipped set, so the settings screen can offer "add the
            # standard reasons" to an account whose list is empty without
            # keeping a second copy of the labels in the client.
            "default_lost_reasons": _default_lost_reasons(),
            "industries": conf.industries or [],
            "lost_reasons": _lost_reason_rows(conf),
            "task_types": conf.task_types or [],
            "auto_link_contacts_by_domain": conf.auto_link_contacts_by_domain,
            "card_display_attributes": conf.card_display_attributes or [],
            # ⚠️ THIS PAYLOAD IS HAND-BUILT WHILE THE PATCH RESPONSE USES
            # `sz.public_dump(settings)`. So a field added to the model shows up
            # on PATCH and silently NOT on GET — two shapes for one resource,
            # and the UI reads GET. Adding `card_show_next_activity` here is
            # what makes the toggle readable at all (CRM-409); a test asserts
            # both endpoints expose it, because this will happen again.
            "card_show_next_activity": bool(
                getattr(conf, "card_show_next_activity", True)),
            "default_pipeline_id": conf.default_pipeline_id,
        }))
    except Exception as ex:  # pylint: disable=broad-except
        # ⚠️ LOG THE CAUSE. A bare 500 from here tells the person nothing and
        # tells the log nothing either; this endpoint is the one every CRM
        # screen loads first, so an opaque failure looks like the whole module
        # is down.
        _logger.exception(f"crm_settings GET failed: {ex}")
        return ResponseSchema(
            status="failure", message="Could not load your CRM settings.", data=None
        )


@router.patch("", response_model=ResponseSchema)
async def update_settings(
    payload: CrmSettingsUpdateRequest,
    ctx: RequestContext = Depends(require("pipelines:write")),
    crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Update the account's CRM settings. CRM-243, extended by CRM-409.

    Only the auto-link fields, for now — see the module docstring.

    ``pipelines:write`` rather than the ``deals:read`` the GET uses: reading a
    picklist is something every rep needs, but changing how the account behaves
    is an admin action. ``editor`` deliberately does not have it (see the RBAC
    matrix), because auto-linking reshapes everyone's company list.

    ⚠️ Turning auto-linking ON records ``auto_link_opted_in``. That flag is
    what stops the one-time rollout migration from switching it back off if it
    is ever re-run — without it, an account that deliberately enabled the
    feature would silently lose it and have no way to tell why.
    """
    account_id = _account_id(ctx)
    settings = await crud.get_or_create(account_id=account_id)
    if settings is None:
        return ResponseSchema(status="failure",
                              message="Could not load your CRM settings.",
                              data=None)

    sent = payload.model_fields_set
    if "auto_link_contacts_by_domain" in sent:
        settings.auto_link_contacts_by_domain = bool(
            payload.auto_link_contacts_by_domain)
        if payload.auto_link_contacts_by_domain:
            settings.auto_link_opted_in = True

    if "lost_reasons" in sent:
        from app.models.settings import LostReason

        rows, error = lr.merge_lost_reasons(
            settings.lost_reasons,
            [r.model_dump() for r in (payload.lost_reasons or [])])
        if error:
            return ResponseSchema(status="failure", message=error, data=None)
        # `id: None` on a new row lets LostReason's default_factory mint one —
        # passing an explicit None would override the factory with null.
        settings.lost_reasons = [
            LostReason(**{k: v for k, v in row.items()
                          if not (k == "id" and v is None)})
            for row in rows]

    if "free_email_domains_blocklist" in sent:
        # Stored normalised so a user typing "@Gmail.com" blocks gmail.com.
        # This list ADDS to the built-in one in auto_link.py — it cannot
        # unblock a provider, which is deliberate: an empty or emptied list
        # must never produce a company called Gmail.
        settings.free_email_domains_blocklist = sorted({
            str(d).strip().lower().lstrip("@")
            for d in (payload.free_email_domains_blocklist or [])
            if str(d).strip()})

    if "card_display_attributes" in sent:
        # CRM-409. Validated against the account's own DEAL attributes, so an
        # unknown or duplicated name is refused with a message rather than
        # filtered out by `card_attributes` later, which would leave a card
        # missing a field and nothing to explain it.
        rows = await attributes.list(account_id=account_id, object_type="deal")
        try:
            settings.card_display_attributes = attr_rules.validate_card_order(
                payload.card_display_attributes or [], rows)
        except AttributeError_ as ex:
            return ResponseSchema(status="failure", message=str(ex), data=None)

    if "card_show_next_activity" in sent:
        settings.card_show_next_activity = bool(payload.card_show_next_activity)

    settings.updated_at = _utcnow()
    await settings.save()
    return _ok(sz.public_dump(settings), "Settings updated.")
