"""CRM attribute definitions API. CRM-012 (read) · CRM-207 (write).

Mounted at ``/api/v1/crm/attributes``::

    GET    ""            the account's attribute definitions
    POST   ""            create a custom attribute
    PATCH  "/{id}"       label, options, visibility, requiredness — never the
                         internal_name or the type
    DELETE "/{id}"       refuses for is_system; needs ?confirm_data_loss=true
                         when records hold a value
    PUT    "/reorder"    form order

WHY THE READ SIDE EXISTS (shipped in E10). A pipeline stage's
``required_attributes`` is a list of ``internal_name`` strings, and the stage
editor's picker must offer exactly the names the server validates against. A
hard-coded client list drifts, and a stage requiring a name nothing can satisfy
traps every deal in it.

WHY THE WRITE SIDE EXISTS (CRM-207). Until it did, custom attributes could be
seeded but never created, and — worse — a stage could require a custom
attribute that no UI could set. Those deals were stuck: the move was refused and
the fill-in dialog had nothing to offer.

PERMISSIONS. Reading is ``pipelines:read``: it is the one CRM permission every
built-in role holds, and the payload is field metadata, not anyone's data.
Writing is ``settings:write``, which only owner/admin hold — an attribute change
reshapes every form in the account, the same reasoning that puts pipeline
structure behind ``pipelines:write``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, Path, Query

from app.config import logger as _logger
from app.crud import (
    CrmContactCrud,
    CompanyCrud, CrmAttributeCrud, DealCrud,
)
from app.models.attribute import CrmAttributeModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.attribute import (
    AttributeCreateRequest, AttributeDetails, AttributeListResponse,
    AttributeReorderRequest, AttributeUpdateRequest,
)
from app.schemas.common import ResponseSchema
from app.services import (
    attributes as rules, defaults, serializers as sz,
)
from app.services.attributes import AttributeError_

router = APIRouter()


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fail(message: str, data: Any = None) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=data)


def _details(row: CrmAttributeModel, value_count: Optional[int] = None) -> AttributeDetails:
    payload = row.model_dump(by_alias=True)
    payload["value_count"] = value_count
    return AttributeDetails.model_validate(payload)


async def _value_count(crud_for, account_id, row: CrmAttributeModel) -> int:
    """How many records hold a value for this attribute.

    Drives the delete confirmation, so it has to be the server's number — a
    client tally would be the page, not the set.
    """
    if row.is_system:
        return 0
    try:
        return await crud_for.count_by({
            "created_by": account_id,
            f"custom_fields.{row.internal_name}": {"$exists": True, "$ne": None},
        })
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        # Unknown is not zero. Returning 0 here would let a delete through the
        # confirmation gate on the strength of a failed query.
        return -1


# ── read ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=ResponseSchema)
async def list_attributes(
    object_type: Optional[str] = Query(
        # ⚠️ `contact` BELONGS HERE AND WAS MISSING, AND THE FAILURE WAS NOT A
        # 404. A pattern mismatch is a FastAPI 422 before the handler runs, so
        # `GET /crm/attributes?object_type=contact&show_on_form=true` — which
        # is exactly what `ContactFormDialog` fetches — came back as
        # "Unprocessable Entity". The dialog then fell through to
        # `attributeFormHelpers`'s hard-coded set, which its own comment calls
        # load-bearing, so the form still RENDERED: nine system fields, and no
        # custom contact attribute ever, with nothing in the UI to say so.
        #
        # Kept as a literal pattern rather than derived from `CrmObjectType`
        # because this is the public contract of a query parameter and the
        # error message should name the values, but it must be changed with the
        # enum. `app/schemas/attribute.py` types the WRITE side as
        # `CrmObjectType`, which is why create/update accepted contact all
        # along and only this read refused it.
        None, pattern="^(deal|company|contact)$",
        description="Omit for every object type",
    ),
    show_on_form: Optional[bool] = Query(
        None, description="Filter to the attributes a create/edit form renders",
    ),
    ctx: RequestContext = Depends(require("pipelines:read")),
    crud: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Attribute definitions, in form order.

    ``is_system`` rows describe the core typed fields (name, amount,
    expected_close_date…); custom attributes live in ``custom_fields`` on the
    record. Both are returned — a stage can require either, and the client must
    not need to know which is which to render a picker or a form.
    """
    account_id = _account_id(ctx)
    await _provision_system(crud, account_id)
    rows = await crud.list(
        account_id=account_id, object_type=object_type, show_on_form=show_on_form
    )
    data = [sz.json_safe(_details(r).model_dump(by_alias=True)) for r in rows]
    return ResponseSchema(status="success", message="", data={
        "data": data, "total": len(data),
    })


async def _provision_system(crud: CrmAttributeCrud, account_id: PyObjectId) -> int:
    """Add any missing ``is_system`` attribute rows. CRM-P0-002.

    WHY THIS IS ON THE READ PATH. ``defaults.build_system_attributes`` used to
    have exactly one caller in the whole codebase — ``seed_crm_defaults.py``.
    Nothing provisioned attributes on demand, so when E25 added a third object
    type every existing account had none of its attributes and the form
    rendered empty. "Remember to run the seed script after a phase that adds a
    field" had failed twice by then, so it is no longer the mechanism.
    ``GET /crm/pipelines`` already self-provisions a default pipeline for the
    same reason; this is that pattern. (That third object type — contacts — has
    since been removed, but the hole it exposed is still here for the next one.)

    ⚠️ THE CHECK IS PER ATTRIBUTE NAME, AND IGNORES THIS REQUEST'S FILTERS.
    "Are there any rows for this query?" is the wrong question twice over: an
    account provisioned for one object type but not another still has rows, and
    a request filtered to the missing type has none while the account is fully
    provisioned for the other. Only a per-name diff across every object type
    gets both cases right.

    Steady state costs one count. Provisioning failure is logged and never
    fails the read — a filtered form is better than a 500, and the count will
    try again on the next request.
    """
    try:
        await _repair_types(crud, account_id)

        expected = defaults.system_attribute_count()
        have = await crud.count_system(account_id=account_id)
        # -1 is "the count failed", which is not the same as "nothing is
        # there". Treating it as zero would re-insert the whole catalogue on
        # top of a complete account every time the count errored.
        if have < 0 or have >= expected:
            return 0

        missing = defaults.missing_system_attributes(
            await crud.system_pairs(account_id=account_id)
        )
        if not missing:
            return 0

        written = await crud.insert_system(account_id=account_id, rows=missing)
        if written:
            _logger.info(
                f"CRM: provisioned {written} system attribute(s) for account "
                f"{account_id} "
                f"({', '.join(sorted({r['object_type'] for r in missing}))})"
            )
        return written
    except Exception as ex:  # pylint: disable=broad-except
        # The whole block, not just the calls that guard themselves — a read
        # that 500s because provisioning failed is strictly worse than a form
        # rendered from the attributes that do exist. Logged, never silent
        # (the activity-write bug in E25 was invisible precisely because it
        # was swallowed without a log line).
        _logger.exception(ex)
        return 0


#: Accounts whose system attribute types this process has already repaired.
#:
#: ⚠️ A CACHE, AND DELIBERATELY A LEAKY ONE. The repair itself is idempotent
#: (see `retype_system`), so this exists only to keep a settled account from
#: paying one `update_many` per attributes read forever. A restart or a second
#: worker simply repeats the repair once and finds nothing — the wrong answer
#: here costs a no-op query, never a wrong type. It is bounded by the number of
#: accounts one process serves, and holds ObjectIds, not records.
_TYPES_REPAIRED: Set[str] = set()


async def _repair_types(crud: CrmAttributeCrud, account_id: PyObjectId) -> int:
    """Fix system attributes seeded with a type the catalogue has since changed.

    ⚠️ THIS SITS OUTSIDE THE COUNT GUARD BELOW, which returns early the moment
    an account has every row it should. Type drift is invisible to a count —
    the row is present, it is simply the wrong type — so folding this in after
    that guard would mean it never ran on exactly the accounts that need it.
    """
    key = str(account_id)
    if key in _TYPES_REPAIRED:
        return 0
    # Marked before the call, not after: a failure is logged inside
    # `retype_system` and retried on the next process, and marking after would
    # retry a permanently failing repair on every single read.
    _TYPES_REPAIRED.add(key)
    changed = await crud.retype_system(
        account_id=account_id, rows=defaults.system_attribute_retypes()
    )
    if changed:
        _logger.info(
            f"CRM: corrected the type of {changed} system attribute(s) for "
            f"account {account_id}"
        )
    return changed


# ── write ────────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[AttributeDetails])
async def create_attribute(
    payload: AttributeCreateRequest,
    ctx: RequestContext = Depends(require("settings:write")),
    crud: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Create a custom attribute.

    ``internal_name`` is generated from the label and returned — the client
    never picks it, so it can never choose a key that collides or that Mongo
    cannot store.
    """
    account_id = _account_id(ctx)
    object_type = getattr(payload.object_type, "value", payload.object_type)
    body = payload.model_dump()
    existing = await crud.list(account_id=account_id, object_type=object_type)
    taken = [r.internal_name for r in existing]

    try:
        # Fill the option keys BEFORE validating: a client sends options as
        # labels only, and validate_options rightly rejects a keyless option.
        # Validating first would fail every select attribute created through
        # the normal path.
        options = _fill_option_keys(body.get("options") or [])
        body["options"] = options
        rules.validate_definition(body, is_create=True)
        internal_name = rules.unique_internal_name(payload.label, taken)
    except AttributeError_ as ex:
        return _fail(str(ex))

    row = CrmAttributeModel(
        created_by=account_id,
        object_type=object_type,
        internal_name=internal_name,
        label=payload.label.strip(),
        attribute_type=getattr(payload.attribute_type, "value", payload.attribute_type),
        description=payload.description,
        options=options,
        is_required=payload.is_required,
        is_readonly=payload.is_readonly,
        is_system=False,
        show_on_form=payload.show_on_form,
        show_on_card=payload.show_on_card,
        group=payload.group,
        form_order=rules.next_form_order(existing),
    )
    created = await crud.insert(row)
    if not created:
        return _fail("Could not create that attribute.")
    return ResponseSchema(status="success", message="Attribute created.",
                          data=_details(created, value_count=0))


def _fill_option_keys(options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Generate a key from the label where one wasn't supplied, uniquely."""
    out: List[Dict[str, Any]] = []
    seen: List[str] = []
    for option in options:
        key = str(option.get("key") or "").strip()
        if not key:
            key = rules.unique_internal_name(option.get("label", ""), seen)
        seen.append(key)
        out.append({"key": key, "label": option.get("label"),
                    "color": option.get("color")})
    return out


@router.patch("/{attribute_id}", response_model=ResponseSchema[AttributeDetails])
async def update_attribute(
    payload: AttributeUpdateRequest,
    attribute_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("settings:write")),
    crud: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Update an attribute's presentation. Never its identity or its type.

    ``AttributeUpdateRequest`` has no ``internal_name`` or ``attribute_type``
    and forbids extras, so a client sending one gets a 422. ``assert_mutable``
    is the second layer for anything reaching the service another way.
    """
    account_id = _account_id(ctx)
    row = await crud.get_scoped(account_id, attribute_id)
    if not row:
        return _fail("That attribute no longer exists.")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return ResponseSchema(status="success", message="", data=_details(row))

    try:
        rules.assert_mutable(row, updates)
        rules.validate_definition(updates, is_create=False)
        if "options" in updates and updates["options"] is not None:
            new_options = _fill_option_keys(updates["options"])
            rules.assert_option_keys_stable(row.options, new_options)
            rules.validate_options(new_options)
            updates["options"] = new_options
    except AttributeError_ as ex:
        return _fail(str(ex))

    # A system attribute may be relabelled, reordered, hidden and marked
    # required — that is the whole point of exposing core fields this way —
    # but never retyped or renamed, which assert_mutable already enforced.
    for field, value in updates.items():
        setattr(row, field, value)
    row.updated_at = _utcnow()
    await row.save()
    return ResponseSchema(status="success", message="Attribute updated.",
                          data=_details(row))


@router.delete("/{attribute_id}", response_model=ResponseSchema)
async def delete_attribute(
    attribute_id: PyObjectId = Path(...),
    confirm_data_loss: bool = Query(
        False, description="Required when records hold a value for this attribute",
    ),
    ctx: RequestContext = Depends(require("settings:write")),
    crud: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
    contacts: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
):
    """Delete a custom attribute, and every value stored under it.

    The values are NOT swept from ``custom_fields`` here. They are left in
    place deliberately: the delete is reversible by recreating an attribute
    with the same generated ``internal_name``, and an unbounded multi-document
    ``$unset`` across a large collection is not something to run inside a
    request. A cleanup sweep belongs in a job.
    """
    account_id = _account_id(ctx)
    row = await crud.get_scoped(account_id, attribute_id)
    if not row:
        return _fail("That attribute no longer exists.")

    # getattr(.., "value", ..), never str(): these enums subclass str, so
    # str(CrmObjectType.COMPANY) is "CrmObjectType.COMPANY", not "company".
    row_object_type = getattr(row.object_type, "value", row.object_type)
    # ⚠️ A LOOKUP, NOT `companies if ... == "company" else deals`. That
    # spelling counted a CONTACT attribute's values against the DEALS
    # collection — so the delete confirmation reported 0 records holding a
    # value, and the guard that exists to prevent silent data loss waved it
    # through.
    # ⚠️ AND `contact` WAS MISSING FROM IT, while the comment directly above
    # described the contact case as the reason the lookup exists. With no arm
    # for it, every contact attribute hit the refusal below — so a contact
    # attribute could not be deleted at all, and the message blamed the
    # endpoint rather than naming the real gap.
    object_crud = {"company": companies, "deal": deals,
                   "contact": contacts,
                   }.get(row_object_type)
    if object_crud is None:
        return _fail("That attribute is on an object this endpoint cannot check.")
    count = await _value_count(object_crud, account_id, row)
    if count < 0:
        return _fail("Couldn't check how many records use this attribute. Try again.")

    try:
        rules.can_delete(row, value_count=count, confirmed=confirm_data_loss)
    except AttributeError_ as ex:
        return _fail(str(ex), {"value_count": count})

    await crud.delete_scoped(account_id, attribute_id)
    return ResponseSchema(
        status="success",
        message="Attribute deleted." if not count else
                f"Attribute deleted. {count} stored "
                f"{'value is' if count == 1 else 'values are'} no longer shown.",
        data=None,
    )


@router.put("/reorder", response_model=ResponseSchema)
async def reorder_attributes(
    payload: AttributeReorderRequest,
    ctx: RequestContext = Depends(require("settings:write")),
    crud: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Set form order. Anything omitted keeps its relative place after the
    named ones, so a partial list cannot silently shuffle the rest.
    """
    account_id = _account_id(ctx)
    object_type = getattr(payload.object_type, "value", payload.object_type)
    rows = await crud.list(account_id=account_id, object_type=object_type)

    try:
        plan = rules.apply_order(rows, [str(i) for i in payload.attribute_ids])
    except AttributeError_ as ex:
        return _fail(str(ex))

    by_id = {str(r.id): r for r in rows}
    now = _utcnow()
    for entry in plan:
        row = by_id.get(entry["id"])
        if row is None or row.form_order == entry["form_order"]:
            continue
        row.form_order = entry["form_order"]
        row.updated_at = now
        await row.save()

    refreshed = await crud.list(account_id=account_id, object_type=object_type)
    return ResponseSchema(status="success", message="Order saved.", data={
        "data": [sz.json_safe(_details(r).model_dump(by_alias=True)) for r in refreshed],
        "total": len(refreshed),
    })
