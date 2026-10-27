"""CRM contacts — a contact as a first-class CRM object. E25.

Mounted at ``/api/v1/crm/contacts``::

    GET    ""                   the list (incl. the E22 advanced filter)
    GET    "/filter-fields"     the field catalogue the filter builder reads
    POST   ""                   create
    GET    "/{contact_id}"      detail
    PATCH  "/{contact_id}"      update
    DELETE "/{contact_id}"      archive, or hard-delete for an account owner
    POST   "/bulk"              bulk actions over ids or a filter

⚠️ **A DUPLICATE EMAIL IS A REJECTION HERE, NOT A WARNING.** A company's
duplicate domain warns and still creates, because subsidiaries and franchises
legitimately share one — that was a deliberate departure from Brevo. An email
cannot work that way: ``uniq_crm_contact_email`` makes it unique per account,
so "create anyway" is not something the database will honour. The refusal
carries the existing contact's id so the UI can offer "Open existing" rather
than a dead end.

⚠️ **THE DUPLICATE CHECK AND THE STORED VALUE USE THE SAME NORMALISATION**
(``contact_rules.normalize_email``). Checking a raw address against a stored
lowercased one passes, and the insert then dies on the index — turning a
friendly message into a 500. That is the single most likely way to break this
file.

Read is account-wide, as everywhere in this module. WRITING somebody else's
contact needs ``crm:manage_others`` — ``contact_rules.may_write``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.config import logger as _logger, settings
from app.crud import (
    CompanyCrud, CrmActivityCrud, CrmAssociationCrud, CrmAttributeCrud,
    CrmContactCrud, DealCrud,
)
from app.models.contact import CrmContactModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.common import (
    BulkActionRequest, BulkActionResult, ResponseSchema,
)
from app.schemas.contact import (
    ContactCreateRequest, ContactDetails,
    ContactListResponse, ContactUpdateRequest,
)
from app.services import (
    attribute_routing, bulk_rules, contact_rules as cr,
    activity_recorder as ar, permissions as perms, serializers as sz,
)
from app.services.bulk_service import BulkRunner
from app.services.filter_service import catalogue_for, fragment_for

router = APIRouter()

OBJECT = "contact"


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _fail(message: str, data: Any = None) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=data)


def _ok(data: Any = None, message: str = "") -> ResponseSchema:
    return ResponseSchema(status="success", message=message, data=data)


def _actor_name(ctx: RequestContext) -> Optional[str]:
    user = ctx.user
    name = f"{getattr(user,'first_name','') or ''} {getattr(user,'last_name','') or ''}".strip()
    return name or getattr(user, "email", None)


def _actor(ctx: RequestContext) -> dict:
    return {
        "actor_user_id": ctx.user.id,
        "is_account_owner": ctx.is_owner,
        "has_manage_others": ctx.has(perms.MANAGE_OTHERS),
    }


async def _lookup_indexes(crud: CrmContactCrud, rows: List[Any]) -> tuple:
    """One batched query per related collection — never one per row (NFR6)."""
    owner_ids = sz.collect_ids(rows, "owner_id")
    owners: List[Any] = []
    if crud.db_session is not None and owner_ids:
        try:
            owners = await crud.db_session["users"].find(
                {"_id": {"$in": [PyObjectId(i) for i in owner_ids]}}
            ).to_list(length=None)
        except Exception as ex:  # pylint: disable=broad-except
            # Expansion is cosmetic — a failure here must not fail the list.
            _logger.exception(ex)
    return (sz.index_by_id(owners, "_id"),)


def _details(contact: Any, *, owner_index: Optional[Dict[str, Any]] = None,
             duplicate: Any = None) -> ContactDetails:
    owner = sz.user_ref((owner_index or {}).get(str(
        getattr(contact, "owner_id", None) or "")))
    company = None
    if getattr(contact, "primary_company_id", None):
        company = {"id": contact.primary_company_id,
                   "name": getattr(contact, "primary_company_name", None) or "—"}
    payload = cr.payload(contact, owner=owner, company=company)
    if duplicate is not None:
        payload["duplicate"] = duplicate
    return ContactDetails.model_validate(payload)


def _duplicate_key_failure(ex: Exception, email: Optional[str]):
    """The `uniq_crm_contact_email` violation, as a failure envelope.

    ⚠️ WHY THIS EXISTS AT ALL, GIVEN THE PRE-CHECK ABOVE IT. `crud.by_email`
    is a read-then-write, and the index is the only thing that actually
    enforces uniqueness: two concurrent creates of the same address both pass
    the check and one loses at the insert. `CrmCrudBase.insert` logs and
    RE-RAISES, so that loser reached the client as a bare 500 while the
    sequential case got a polite "you already have this contact". Same
    outcome, two different answers, decided by timing.

    ⚠️ AND IT TELLS THE TWO CASES APART. A violation on a row with NO email
    cannot be a duplicate address — it means the collection is still carrying
    the OLD `uniq_crm_contact_email`, built with `sparse=True`, which indexes
    every document and so collides on the second null. `models/contact.py`
    declares it partial for exactly that reason, and a database that predates
    the change keeps the old one because `allow_index_dropping` is False.
    Saying "duplicate email" there would send the reader hunting for a
    duplicate that does not exist.
    """
    if not str(ex.__class__.__name__) == "DuplicateKeyError":
        return None
    if email:
        return _fail("A contact with that email address already exists in "
                     "this account.")
    return _fail(
        "This account's crm_contacts collection is still using the OLD "
        "unique email index (sparse), which rejects a second contact with no "
        "email address. Run scripts/check_crm_contact_index.py --apply and "
        "restart the service.")


async def _route_values(attributes_crud, account_id, values):
    """Split `custom_fields` into typed columns and custom fields.

    Same path companies and deals take — one validation rule, three callers,
    so a contact arriving by import cannot behave differently from one typed
    in.
    """
    return await attribute_routing.route_for(
        attributes_crud, account_id, OBJECT, values)


# ── list ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=ContactListResponse)
async def list_contacts(
    q: Optional[str] = Query(None, description="Search name, email and company"),
    owner_id: Optional[PyObjectId] = Query(None),
    company_id: Optional[PyObjectId] = Query(None),
    lifecycle_stage: Optional[str] = Query(None),
    tag_ids: Optional[List[PyObjectId]] = Query(None),
    linked: Optional[bool] = Query(
        None, description="True = in the marketing audience, False = CRM-only"),
    is_archived: Optional[bool] = Query(False),
    created_from: Optional[datetime] = Query(None),
    created_to: Optional[datetime] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=200),
    filters_json: Optional[str] = Query(
        None, alias="filters",
        description="A JSON filter group from the advanced filter builder "
                    '(E22): {"match": "all", "conditions": [...]}'),
    ctx: RequestContext = Depends(require("contacts:read")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """The contacts list. Sorted by name ASCENDING by default.

    Unlike companies and deals this list defaults to ascending: it is read as
    a directory, and newest-first makes looking somebody up a scroll.
    """
    account_id = _account_id(ctx)

    extra, error = await fragment_for(attributes, account_id, OBJECT,
                                      filters_json)
    if error:
        return ContactListResponse(data=[], total=0, status="failure",
                                   message=error)

    filters = dict(q=q, owner_id=owner_id, company_id=company_id,
                   lifecycle_stage=lifecycle_stage, tag_ids=tag_ids,
                   linked=linked, is_archived=is_archived,
                   created_from=created_from, created_to=created_to,
                   extra=extra)
    rows = await crud.list(account_id=account_id, sort_by=sort_by,
                           sort_dir=sort_dir, skip=skip, limit=limit, **filters)
    # Same builder, same arguments — the count cannot disagree with the page.
    total = await crud.count(account_id=account_id, **filters)

    (owner_index,) = await _lookup_indexes(crud, rows)
    data = [_details(r, owner_index=owner_index) for r in rows]
    return ContactListResponse(data=data, total=total, status="success")


# ── filter catalogue ─────────────────────────────────────────────────────────
# Declared ABOVE `GET /{contact_id}`: FastAPI matches in declaration order, and
# a PyObjectId path parameter would capture "filter-fields" and 422 on it.
@router.get("/filter-fields", response_model=ResponseSchema)
async def contact_filter_fields(
    ctx: RequestContext = Depends(require("contacts:read")),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Every field a contact can be filtered on, with its operators."""
    fields = await catalogue_for(attributes, _account_id(ctx), OBJECT)
    return _ok({"fields": fields, "total": len(fields)})


# ── create ───────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[ContactDetails])
async def create_contact(
    payload: ContactCreateRequest,
    ctx: RequestContext = Depends(require("contacts:write")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Create a contact.

    The contact is CRM-ONLY: nothing is written to the platform `contacts`
    collection here. It becomes marketable when somebody adds it to a list
    (CRM-322), which is the one moment a `subscribe_date` can honestly be set.
    """
    account_id = _account_id(ctx)
    values = payload.model_dump(exclude_unset=True)

    # ⚠️ ROUTE BEFORE VALIDATING, AND THIS ORDER IS THE WHOLE BUG THAT WAS
    # HERE. `ContactFormDialog` sends ONE flat map under `custom_fields` and
    # relies on the server to split system names onto typed columns — its
    # docstring says so, and `_route_values` below is that split. But
    # `validate` and `has_identity` used to run on the TOP-LEVEL dump only, so
    # a payload of `{"custom_fields": {"first_name": "demo"}}` was refused with
    # "A contact needs a name or an email address" while carrying a perfectly
    # good name one level down. Every contact created from the UI form hit it.
    #
    # The author knew routing can set identity fields — the `full_name`
    # comment further down says the display name must be derived AFTER routing
    # for exactly that reason. The CHECK simply never moved with it.
    #
    # ⚠️ AND IT MATTERED MORE THAN A BAD MESSAGE. The duplicate lookup below
    # read `payload.email`, which is None when the address arrives nested — so
    # a nested email skipped the per-account duplicate check entirely and went
    # straight at the unique index, turning a friendly "you already have this
    # contact" into a 500.
    #
    # Why this was never caught: the server's own tests posted FLAT payloads
    # and the UI posted nested ones. Neither side was wrong on its own.
    typed, custom, route_error = await _route_values(
        attributes, account_id, payload.custom_fields)
    if route_error:
        return _fail(route_error)

    # The identity fields as they will actually be stored: top-level first, a
    # routed system name winning, because that is what the model ends up with.
    merged = {k: v for k, v in values.items() if k != "custom_fields"}
    for field, value in typed.items():
        if value is not None:
            merged[field] = value

    error = cr.validate(merged)
    if error:
        return _fail(error)
    if not cr.has_identity(merged):
        # ⚠️ SAY WHAT ARRIVED, NOT JUST WHAT IS WANTED. The bare sentence cost
        # a debugging round trip: the payload LOOKED right and the refusal gave
        # no way to tell "you sent nothing" from "you sent empty strings" from
        # "the body never reached me" — three different mistakes in the client,
        # with one message between them.
        #
        # The three identity keys only. Never echo the VALUES back: this is an
        # unauthenticated-shaped refusal that may be logged or shown, and
        # repeating a submitted email into it is how a reflected value ends up
        # somewhere it should not be.
        seen = [k for k in ("first_name", "last_name", "email")
                if k in merged]
        if not seen:
            detail = ("the request body carried none of them — check it is "
                      "attached and sent as application/json")
        else:
            blank = [k for k in seen if not str(values.get(k) or "").strip()]
            detail = (f"{', '.join(blank)} arrived empty"
                      if blank else f"{', '.join(seen)} arrived unusable")
        return _fail(f"A contact needs a name or an email address: {detail}.")

    email = cr.normalize_email(merged.get("email"))
    if email:
        # ⚠️ THE SAME NORMALISATION THE COLUMN STORES. See the module docstring.
        existing = await crud.by_email(account_id=account_id, email=email)
        if existing is not None:
            return _fail(
                cr.duplicate_message(existing),
                {"duplicate": {"id": existing.id,
                               "full_name": existing.full_name,
                               "email": existing.email}})

    first = cr.clean_name(merged.get("first_name"))[0]
    last = cr.clean_name(merged.get("last_name"))[0]
    lifecycle = cr.clean_lifecycle(merged.get("lifecycle_stage"))[0]

    company_name = None
    if payload.primary_company_id:
        company = await companies.get_scoped(account_id,
                                             payload.primary_company_id)
        if company is None:
            return _fail("That company no longer exists.")
        company_name = company.name

    contact = CrmContactModel(
        created_by=account_id,
        created_by_user_id=ctx.user.id,
        owner_id=payload.owner_id or ctx.user.id,
        email=email,
        first_name=first,
        last_name=last,
        full_name=cr.display_name(first, last, email),
        phone=merged.get("phone"),
        phone_country_code=merged.get("phone_country_code"),
        job_title=cr.clean_name(merged.get("job_title"))[0],
        linkedin_url=(str(merged["linkedin_url"])
                      if merged.get("linkedin_url") else None),
        lifecycle_stage=lifecycle,
        primary_company_id=payload.primary_company_id,
        primary_company_name=company_name,
        tag_ids=payload.tag_ids or [],
        description=merged.get("description"),
        custom_fields={},
        source="manual",
    )

    # `typed` / `custom` came from the split at the top of this handler — it
    # is not run twice. The loop still applies the routed values, because
    # `merged` only drives the fields named explicitly above and a typed column
    # added to `SYSTEM_FIELDS["contact"]` later must land without an edit here.
    for field, value in typed.items():
        if value is not None:
            setattr(contact, field, value)
    contact.custom_fields = custom
    # A routed system name can change the identity fields, so the denormalised
    # display name is derived AFTER routing, never before.
    contact.full_name = cr.display_name(contact.first_name, contact.last_name,
                                        contact.email)

    try:
        created = await crud.insert(contact)
    except Exception as ex:  # pylint: disable=broad-except
        # Only the uniqueness violation is turned into an answer; anything else
        # is still a 500, because an unknown database failure is not something
        # to paper over with a friendly sentence.
        failure = _duplicate_key_failure(ex, email)
        if failure is None:
            raise
        return failure
    if not created:
        return _fail("Could not create that contact.")

    # ⚠️ `ar.build_activity`, NOT a hand-rolled dict. `CrmActivityModel`
    # requires `title`; a dict with a `summary` key fails validation inside
    # `activity.write`, which CATCHES and LOGS — so every contact's timeline
    # was silently empty and no test could see it.
    await ar.record(activity.write, ar.build_activity(
        account_id=account_id, entity_type=OBJECT, entity_id=created.id,
        activity_type="created", title=f"{created.full_name} created",
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    (owner_index,) = await _lookup_indexes(crud, [created])
    return ResponseSchema(status="success",
                          message=f"{created.full_name} created.",
                          data=_details(created, owner_index=owner_index))


# ── detail ───────────────────────────────────────────────────────────────────
@router.get("/{contact_id}", response_model=ResponseSchema[ContactDetails])
async def get_contact(
    contact_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("contacts:read")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
):
    account_id = _account_id(ctx)
    contact = await crud.get_scoped(account_id, contact_id)
    if not contact:
        return _fail("That contact no longer exists.")
    (owner_index,) = await _lookup_indexes(crud, [contact])
    return _ok(_details(contact, owner_index=owner_index))


# ── update ───────────────────────────────────────────────────────────────────
@router.patch("/{contact_id}", response_model=ResponseSchema[ContactDetails])
async def update_contact(
    payload: ContactUpdateRequest,
    contact_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("contacts:write")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Update a contact.

    ⚠️ `exclude_unset`, not a None check: clearing the job title or detaching
    the company is a real edit, and a None check would read it as "unchanged"
    so the button would silently do nothing.
    """
    account_id = _account_id(ctx)
    contact = await crud.get_scoped(account_id, contact_id)
    if not contact:
        return _fail("That contact no longer exists.")
    if not cr.may_write(contact, **_actor(ctx)):
        return _fail(cr.write_refusal())

    changes = payload.model_dump(exclude_unset=True)

    # ⚠️ ROUTE FIRST, SAME AS `create_contact`, AND FOR THREE REASONS HERE
    # RATHER THAN ONE. `ContactFormDialog` edits through the same flat
    # `custom_fields` map it creates with, and the routing used to happen at
    # the BOTTOM of this handler — so a form edit arrived as
    # `{"custom_fields": {...}}` and:
    #
    #   1. `cr.validate` below saw an empty `changes` and validated nothing —
    #      a bad `lifecycle_stage` or a malformed email passed straight
    #      through;
    #   2. the duplicate-email check below never ran, because `"email" in
    #      changes` was False, so changing an address to one the account
    #      already held went at the unique index and returned a 500 instead of
    #      "you already have this contact";
    #   3. the routed values were applied with a bare `setattr`, AFTER the
    #      per-field blocks, so they skipped `clean_name` / `clean_lifecycle`
    #      entirely and an uncleaned value overwrote a cleaned one.
    #
    # Merging the routed system names into `changes` fixes all three at once:
    # every `if field in changes` block below now sees them and cleans them.
    typed, custom, route_error = await _route_values(
        attributes, account_id, changes.get("custom_fields"))
    if route_error:
        return _fail(route_error)
    changes.pop("custom_fields", None)
    changes.update(typed)

    error = cr.validate(changes)
    if error:
        return _fail(error)

    if "email" in changes:
        email = cr.normalize_email(changes["email"])
        if email and email != contact.email:
            existing = await crud.by_email(account_id=account_id, email=email)
            if existing is not None and str(existing.id) != str(contact.id):
                return _fail(
                    cr.duplicate_message(existing),
                    {"duplicate": {"id": existing.id,
                                   "full_name": existing.full_name,
                                   "email": existing.email}})
        contact.email = email

    for field in ("first_name", "last_name", "job_title"):
        if field in changes:
            setattr(contact, field, cr.clean_name(changes[field])[0])

    if "lifecycle_stage" in changes:
        contact.lifecycle_stage = cr.clean_lifecycle(
            changes["lifecycle_stage"])[0]

    if "primary_company_id" in changes:
        company_id = changes["primary_company_id"]
        if company_id:
            company = await companies.get_scoped(account_id, company_id)
            if company is None:
                return _fail("That company no longer exists.")
            contact.primary_company_id = company.id
            contact.primary_company_name = company.name
        else:
            # Detaching clears BOTH, or the list keeps showing a company the
            # contact is no longer attached to.
            contact.primary_company_id = None
            contact.primary_company_name = None

    for field in ("phone", "phone_country_code", "description", "owner_id",
                  "is_archived"):
        if field in changes:
            setattr(contact, field, changes[field])
    if "linkedin_url" in changes:
        contact.linkedin_url = (str(changes["linkedin_url"])
                                if changes["linkedin_url"] else None)
    if "tag_ids" in changes:
        contact.tag_ids = list(changes["tag_ids"] or [])

    if custom:
        # ⚠️ NO `setattr` LOOP ANY MORE, AND NO SECOND ROUTING CALL. The split
        # happened at the top and the typed values were merged into `changes`,
        # so the per-field blocks above have already applied them THROUGH the
        # cleaners. Re-applying them here would undo that cleaning, because
        # this runs last.
        #
        # MERGED, not replaced: a form that renders a subset of the account's
        # attributes would otherwise wipe every key it did not show.
        merged = dict(contact.custom_fields or {})
        merged.update(custom)
        contact.custom_fields = merged

    # Always re-derived: any of the three inputs may have moved.
    contact.full_name = cr.display_name(contact.first_name, contact.last_name,
                                        contact.email)
    contact.updated_at = datetime.now(timezone.utc)
    await contact.save()

    await ar.record(activity.write, ar.build_activity(
        account_id=account_id, entity_type=OBJECT, entity_id=contact.id,
        activity_type="updated", title=f"{contact.full_name} updated",
        metadata={"fields": sorted(changes)},
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    (owner_index,) = await _lookup_indexes(crud, [contact])
    return _ok(_details(contact, owner_index=owner_index), "Contact saved.")


# ── delete ───────────────────────────────────────────────────────────────────
@router.delete("/{contact_id}", response_model=ResponseSchema)
async def delete_contact(
    contact_id: PyObjectId = Path(...),
    hard: bool = Query(False, description="Permanently delete (owner/admin only)"),
    ctx: RequestContext = Depends(require("contacts:write")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Archive by default; hard-delete only for an account owner.

    ⚠️ A HARD DELETE DOES NOT TOUCH THE PLATFORM CONTACT. If this person was
    added to the marketing audience they are a subscriber with their own
    consent record, and deleting a CRM row is not consent to remove them from
    a mailing list — that is a different decision, made on that screen.
    """
    account_id = _account_id(ctx)
    contact = await crud.get_scoped(account_id, contact_id)
    if not contact:
        return _fail("That contact no longer exists.")
    if not cr.may_write(contact, **_actor(ctx)):
        return _fail(cr.write_refusal())
    if hard and not ctx.is_owner and not ctx.has(perms.MANAGE_OTHERS):
        return _fail("Only an account owner can permanently delete a contact.")

    name = contact.full_name
    if hard:
        await assoc.delete_all_for(account_id=account_id, entity_type=OBJECT,
                                   entity_id=contact.id)
        ok = await crud.delete_scoped(account_id, contact_id)
        if not ok:
            return _fail("Could not delete that contact.")
        message = f"{name} deleted."
    else:
        contact.is_archived = True
        contact.updated_at = datetime.now(timezone.utc)
        await contact.save()
        message = f"{name} archived."

    await ar.record(activity.write, ar.build_activity(
        account_id=account_id, entity_type=OBJECT, entity_id=contact_id,
        activity_type="deleted" if hard else "archived", title=message,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))
    return _ok({"id": str(contact_id)}, message)


# ── no marketing link ────────────────────────────────────────────────────────
# `POST /{id}/link` and `DELETE /{id}/link` lived here (CRM-322). They are
# gone, with `contact_link_service` and the `contact_id` field: the CRM reads
# and writes `crm_contacts` and nothing else. See the model docstring for why,
# and `docs/CRM/api-contract.md` for what a client should do instead.

# ── bulk ─────────────────────────────────────────────────────────────────────
@router.post("/bulk", response_model=ResponseSchema[BulkActionResult])
async def bulk_contacts(
    payload: BulkActionRequest,
    hard: bool = Query(False, description="Permanently delete (owner/admin only)"),
    ctx: RequestContext = Depends(require("contacts:write")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """One action over many contacts. Takes EITHER ids OR filters, never both.

    A partial result is a success: HTTP 200 with the counts, because a bulk
    operation that half-worked did half-work and a failure would tell the
    client to retry the whole thing.
    """
    account_id = _account_id(ctx)
    if hard and not ctx.is_owner and not ctx.has(perms.MANAGE_OTHERS):
        return _fail("Only an account owner can permanently delete records.")

    # The advanced filter reaches bulk as the GROUP, compiled here — never as a
    # fragment from the client. See `bulk_rules.BULK_FILTER_KEYS`.
    extra, error = await fragment_for(attributes, account_id, OBJECT,
                                      payload.filter_group)
    if error:
        return _fail(error)

    runner = BulkRunner(
        entity_type=OBJECT, account_id=account_id, crud=crud,
        activity=activity, actor_user_id=ctx.user.id,
        actor_name=_actor_name(ctx),
        is_account_owner=ctx.is_owner,
        has_manage_others=ctx.has(perms.MANAGE_OTHERS),
    )
    try:
        result = await runner.run(action=payload.action, payload=payload.payload,
                                  ids=payload.ids, filters=payload.filters,
                                  hard=hard, extra=extra)
    except bulk_rules.BulkError as ex:
        return _fail(str(ex))

    result.pop("cap_notice", None)
    message = result.pop("message", "")
    return ResponseSchema(status="success", message=message,
                          data=BulkActionResult.model_validate(result))


# ── the timeline (CRM-402) ───────────────────────────────────────────────────
@router.get("/{contact_id}/activities", response_model=ResponseSchema)
async def contact_activities(
    contact_id: PyObjectId = Path(...),
    activity_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(30, ge=0, le=100),
    ctx: RequestContext = Depends(require("contacts:read")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """This contact's history, newest first.

    Mirrors `GET /crm/companies/{id}/activities` and
    `GET /crm/deals/{id}/activities` exactly — same nested envelope, same
    `skip`/`limit`, same `activity_type` filter — so one `ActivityTimeline`
    component serves all three.

    ⚠️ ADDED IN CRM-402, NOT E25. Every write path in this module has been
    recording contact activity since E25 (created, updated, archived, linked,
    unlinked, bulk), and there was no endpoint to read any of it: the rows were
    being written into a collection nothing could query per contact. The detail
    page's Activity tab is the first reader.

    Nested `data: {data, total}`, like its two siblings — the client needs the
    true total to page, and `skip` is the only way older history is reachable.
    """
    account_id = _account_id(ctx)
    if not await crud.get_scoped(account_id, contact_id):
        return _fail("That contact no longer exists.")
    rows = await activity.list(account_id=account_id, entity_type=OBJECT,
                               entity_id=contact_id,
                               activity_type=activity_type,
                               skip=skip, limit=limit)
    total = await activity.count(account_id=account_id, entity_type=OBJECT,
                                 entity_id=contact_id,
                                 activity_type=activity_type)
    return _ok({"data": [sz.public_dump(r) for r in rows], "total": total})
