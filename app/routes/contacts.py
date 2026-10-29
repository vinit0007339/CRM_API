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
    CrmContactCrud, CrmContactProfileCrud, CrmSettingsCrud, DealCrud,
)
from app.models.contact import CrmContactModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.common import (
    BulkActionRequest, BulkActionResult, ResponseSchema,
)
from app.schemas.contact import (
    ContactCreateRequest, ContactDetails, ContactListResponse,
    ContactUpdateRequest,
)
from app import foreign, host_writes
from app.services import (
    associations as assoc_rules, attribute_routing, bulk_rules,
    contact_rules as cr, contact_scope, activity_recorder as ar,
    permissions as perms, serializers as sz,
)
from app.services.bulk_service import BulkRunner
from app.services.filter_service import catalogue_for, fragment_for

router = APIRouter()

OBJECT = "contact"
# Linked records embedded in the detail response. Same value as
# `companies.LINKED_PREVIEW`, deliberately: the two detail pages show the same
# "Showing N of M" line and a different ceiling on each would be a bug nobody
# would think to look for.
LINKED_PREVIEW = 20


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
             duplicate: Any = None,
             linked_deals: Optional[Dict[str, Any]] = None,
             linked_companies: Optional[Dict[str, Any]] = None) -> ContactDetails:
    owner = sz.user_ref((owner_index or {}).get(str(
        getattr(contact, "owner_id", None) or "")))
    company = None
    if getattr(contact, "primary_company_id", None):
        company = {"id": contact.primary_company_id,
                   "name": getattr(contact, "primary_company_name", None) or "—"}
    payload = cr.payload(contact, owner=owner, company=company,
                         linked_deals=linked_deals,
                         linked_companies=linked_companies)
    if duplicate is not None:
        payload["duplicate"] = duplicate
    return ContactDetails.model_validate(payload)


def _unified_details(contact: Any, profile: Any, *,
                     owner_index: Optional[Dict[str, Any]] = None,
                     linked_deals: Optional[Dict[str, Any]] = None,
                     linked_companies: Optional[Dict[str, Any]] = None
                     ) -> ContactDetails:
    """`ContactDetails` from the person plus this account's profile of them.

    The unified twin of `_details`. Both return the same model with the same
    keys — see `contact_rules.unified_payload` for why that is a requirement
    and not a coincidence.
    """
    owner = sz.user_ref((owner_index or {}).get(str(
        cr._get(profile, "owner_id") or "")))
    company = None
    company_id = cr._get(profile, "primary_company_id")
    if company_id:
        company = {"id": company_id,
                   "name": cr._get(profile, "primary_company_name") or "—"}
    return ContactDetails.model_validate(cr.unified_payload(
        contact, profile, owner=owner, company=company,
        linked_deals=linked_deals, linked_companies=linked_companies))


async def _owner_index_for(crud: Any, rows: List[Any]) -> Dict[str, Any]:
    """Owner expansion for profile rows — the same batched read as
    `_lookup_indexes`, which is typed to the contact CRUD."""
    owner_ids = sz.collect_ids(rows, "owner_id")
    owners: List[Any] = []
    if crud.db_session is not None and owner_ids:
        try:
            owners = await crud.db_session["users"].find(
                {"_id": {"$in": [PyObjectId(i) for i in owner_ids]}}
            ).to_list(length=None)
        except Exception as ex:  # pylint: disable=broad-except
            # Expansion is cosmetic — a failure must not fail the list.
            _logger.exception(ex)
    return sz.index_by_id(owners, "_id")


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
    scope: str = Query(
        "worked", pattern="^(worked|all)$",
        description='Unified path only. "worked" (default) = contacts this '
                    'account has a CRM profile for. "all" = the whole '
                    "marketing audience. Ignored on the legacy path, which "
                    "has only one population."),
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
    profiles: CrmContactProfileCrud = Depends(get_crud(CrmContactProfileCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """The contacts list. Sorted by name ASCENDING by default.

    Unlike companies and deals this list defaults to ascending: it is read as
    a directory, and newest-first makes looking somebody up a scroll.

    Two implementations, chosen by `contact_scope.unified` — see
    `docs/contact-unification-plan.md`. Both return the same
    `ContactListResponse`.
    """
    account_id = _account_id(ctx)

    extra, error = await fragment_for(attributes, account_id, OBJECT,
                                      filters_json)
    if error:
        return ContactListResponse(data=[], total=0, status="failure",
                                   message=error)

    conf = await conf_crud.get_or_create(account_id=account_id)
    if contact_scope.unified(conf) and scope == "all":
        # ⚠️ DRIVEN FROM `contacts`, WHICH INVERTS WHICH HALF IS INDEX-SERVED.
        # Plan decision 0.2: this view exists to FIND somebody in the audience,
        # so name and email search are the thing that must work, and they are
        # served by the host. Owner and lifecycle live on the profile and are
        # therefore NOT filterable here — they would need a join that does not
        # exist across the host boundary.
        #
        # ⚠️ SO THOSE FILTERS ARE REFUSED, NOT IGNORED. Silently returning the
        # unfiltered audience when somebody filters by owner is the failure
        # that reads as "the filter does nothing" and gets debugged for an
        # afternoon.
        relational = [name for name, value in (
            ("owner_id", owner_id), ("company_id", company_id),
            ("lifecycle_stage", lifecycle_stage), ("tag_ids", tag_ids),
            ("filters", extra),
        ) if value]
        if relational:
            return ContactListResponse(
                data=[], total=0, status="failure",
                message=("All contacts cannot be filtered by "
                         + ", ".join(relational)
                         + " — those describe your CRM relationship and only "
                           "exist for contacts you have worked. Switch to the "
                           "default view to use them."))

        people, total = await foreign.contacts_page(
            crud.db_session, account_id, q=q, skip=skip, limit=limit,
            sort=[("first_name", 1), ("last_name", 1)])
        # One batched profile read for the page — the mirror of the default
        # view, which batches the people instead.
        by_contact = await profiles.for_contacts(
            account_id=account_id,
            contact_ids=[p.get("_id") for p in people])
        owner_index = await _owner_index_for(profiles,
                                             list(by_contact.values()))
        data = [
            _unified_details(person, by_contact.get(str(person.get("_id"))),
                             owner_index=owner_index)
            for person in people
        ]
        return ContactListResponse(data=data, total=total, status="success")

    if contact_scope.unified(conf):
        # ⚠️ DRIVEN FROM `crm_contact_profiles`, PER PLAN DECISION 0.2. Owner,
        # lifecycle, company and the name sort are all index-served here. The
        # people are then expanded in ONE batched read through the host door,
        # never one per row.
        #
        # ⚠️ `linked` IS DROPPED, NOT PASSED. On `crm_contacts` it asked
        # whether a CRM person had been joined to the marketing audience; here
        # every row already is one, so the question has no answer. Passing it
        # through would filter on a field this collection does not have and
        # silently return nothing.
        rows = await profiles.list(
            account_id=account_id, sort_by=sort_by, sort_dir=sort_dir,
            skip=skip, limit=limit,
            q=q, owner_id=owner_id, company_id=company_id,
            lifecycle_stage=lifecycle_stage, tag_ids=tag_ids,
            is_archived=is_archived, created_from=created_from,
            created_to=created_to, extra=extra)
        total = await profiles.count(
            account_id=account_id,
            q=q, owner_id=owner_id, company_id=company_id,
            lifecycle_stage=lifecycle_stage, tag_ids=tag_ids,
            is_archived=is_archived, created_from=created_from,
            created_to=created_to, extra=extra)

        people = await foreign.contacts_by_ids(
            crud.db_session, [r.contact_id for r in rows])
        by_id = sz.index_by_id(people, "_id")
        owner_index = await _owner_index_for(profiles, rows)
        data = [
            _unified_details(by_id.get(str(r.contact_id)) or {}, r,
                             owner_index=owner_index)
            for r in rows
        ]
        return ContactListResponse(data=data, total=total, status="success")

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
@router.get("/by-platform/{platform_contact_id}", response_model=ResponseSchema)
async def platform_contact_crm_summary(
    platform_contact_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("contacts:read")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """The CRM's view of a PLATFORM contact: its deals, companies and history.

    ⚠️ **DECLARED BEFORE `/{contact_id}`, AND THAT IS LOAD-BEARING.** FastAPI
    matches in declaration order, so registered after it the literal segment
    `by-platform` would be captured as a contact id and this route would never
    be reached — the symptom being a 404 for a contact that plainly exists.

    ⚠️ **IT READS `to_type="contact"`, THE PLATFORM SIDE.** That is the whole
    point: the marketing contact detail page holds a `contacts` id and has no
    `crm_contacts` row to look up. Asking for `crm_contact` here would answer
    empty for every contact on that screen.

    ⚠️ **AND IT IS A SEPARATE PATH THAT PHASE 7 DELETES.** The unified model
    makes `GET /crm/contacts/{id}` answer this for any contact, which is why
    this endpoint is a thin read and holds no logic of its own — see
    `docs/contact-unification-plan.md`. It exists so the marketing contact
    page can show CRM panels before the migration runs, not as a second
    design.
    """
    account_id = _account_id(ctx)

    deal_ids = await assoc.counterparts(
        account_id=account_id, entity_type="contact",
        entity_id=platform_contact_id, want_type="deal")
    company_ids = await assoc.counterparts(
        account_id=account_id, entity_type="contact",
        entity_id=platform_contact_id, want_type="company")

    deal_rows = await deals.list_by(
        {"created_by": account_id, "_id": {"$in": deal_ids[:LINKED_PREVIEW]}},
        limit=LINKED_PREVIEW) if deal_ids else []
    company_rows = await companies.list_by(
        {"created_by": companies.oid(account_id),
         "_id": {"$in": companies.oids(company_ids[:LINKED_PREVIEW])}},
        limit=LINKED_PREVIEW) if company_ids else []

    # Same call the contact activity endpoint makes, with the PLATFORM entity
    # type — `CrmActivityCrud.list` takes the filter builder's kwargs.
    history = await activity.list(
        account_id=account_id, entity_type="contact",
        entity_id=platform_contact_id, limit=LINKED_PREVIEW)

    return _ok(sz.json_safe({
        "linked_deals": sz.linked_collection(
            deal_rows, len(deal_ids),
            lambda d: {"id": d.id, "name": d.name, "amount": d.amount,
                       "status": getattr(d.status, "value", d.status),
                       "stage_id": d.stage_id,
                       "pipeline_id": getattr(d, "pipeline_id", None),
                       "expected_close_date": d.expected_close_date}),
        "linked_companies": sz.linked_collection(
            company_rows, len(company_ids),
            lambda c: {"id": c.id, "name": c.name,
                       "domain": getattr(c, "domain", None),
                       "deal_count": int(getattr(c, "deal_count", 0) or 0)}),
        "activities": [sz.public_dump(a) for a in (history or [])],
    }))


@router.post("", response_model=ResponseSchema[ContactDetails])
async def create_contact(
    payload: ContactCreateRequest,
    ctx: RequestContext = Depends(require("contacts:write")),
    crud: CrmContactCrud = Depends(get_crud(CrmContactCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    profiles: CrmContactProfileCrud = Depends(get_crud(CrmContactProfileCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Create a contact.

    ⚠️ TWO DIFFERENT THINGS, DECIDED BY THE FLAG. On the LEGACY path the
    contact is CRM-only: nothing is written to the platform `contacts`
    collection, and it becomes marketable only when somebody adds it to a list
    (CRM-322) — the one moment a `subscribe_date` can honestly be set.

    On the UNIFIED path this creates a marketing contact, because there is only
    one kind of contact. It is created **unsubscribed and with no
    subscribe_date** (`contact_rules.new_person_fields`): the person exists in
    the audience but has consented to nothing, which is the honest
    representation of a sales lead.
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

    conf = await conf_crud.get_or_create(account_id=account_id)
    if contact_scope.unified(conf):
        # ⚠️ CREATING A CONTACT HERE CREATES A MARKETING CONTACT. That is the
        # whole point of the unification — and the reason `new_person_fields`
        # forces `subscribed=False` and stamps no `subscribe_date`. Putting
        # somebody in a pipeline is not them asking to be emailed.
        # Initialised in this branch: the legacy path's own `company_name = None`
        # sits BELOW it, so relying on that would be a NameError whenever no
        # company was supplied.
        company_name = None
        if payload.primary_company_id:
            company = await companies.get_scoped(account_id,
                                                 payload.primary_company_id)
            if company is None:
                return _fail("That company no longer exists.")
            company_name = company.name

        person_fields = cr.new_person_fields({
            **merged, "email": email, "first_name": first, "last_name": last,
            "job_title": cr.clean_name(merged.get("job_title"))[0],
        })
        try:
            person, _created = await host_writes.find_or_create_contact(
                crud.db_session, account_id, email, person_fields)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return _fail("Could not create that contact in the audience.")
        if not person:
            return _fail("Could not create that contact.")

        contact_oid = person.get("_id")
        # ⚠️ THE DUPLICATE CHECK IS AGAINST THE PROFILE, NOT THE PERSON. An
        # address already in the marketing audience is NOT a duplicate — it is
        # the normal case, and refusing it would make the unified CRM unable
        # to work anybody marketing found first. Having already WORKED them is
        # the duplicate.
        existing_profile = await profiles.for_contact(
            account_id=account_id, contact_id=contact_oid)
        if existing_profile is not None:
            return _fail(
                "You are already working this contact.",
                {"duplicate": {"id": contact_oid,
                               "full_name": existing_profile.full_name,
                               "email": existing_profile.email}})

        profile = await profiles.ensure(
            account_id=account_id, contact_id=contact_oid,
            actor_user_id=ctx.user.id,
            defaults={
                "owner_id": payload.owner_id or ctx.user.id,
                "lifecycle_stage": lifecycle,
                "primary_company_id": payload.primary_company_id,
                "primary_company_name": company_name,
                "tag_ids": payload.tag_ids or [],
                "description": merged.get("description"),
                "custom_fields": custom,
                "source": "manual",
                "full_name": cr.display_name(first, last, email),
                "email": email,
                "job_title": cr.clean_name(merged.get("job_title"))[0],
            })
        if profile is None:
            return _fail("Could not start working that contact.")

        await ar.record(activity.write, ar.build_activity(
            account_id=account_id, entity_type="contact",
            entity_id=contact_oid, activity_type="contact_created",
            title=profile.full_name or "Contact created",
            actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))
        owner_index = await _owner_index_for(profiles, [profile])
        return _ok(_unified_details(person, profile, owner_index=owner_index),
                   "Contact created.")

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
    assoc: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    profiles: CrmContactProfileCrud = Depends(get_crud(CrmContactProfileCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """One request paints the whole detail page — the same contract as
    `GET /companies/{id}`.

    ⚠️ THE ASSOCIATION SIDE DEPENDS ON THE PATH, AND GETTING IT WRONG IS
    SILENT. On the legacy path a CRM-owned person is `to_type="crm_contact"`;
    `to_type="contact"` is a PLATFORM contact in a different collection. On the
    unified path the person IS the platform contact, so it is `"contact"`.
    Asking for the wrong one returns an empty list on a contact that plainly
    has deals — a broken page rather than a visible error. See
    `models/contact.py`, `_fetch_crm_contacts` in `routes/companies.py`, and
    `docs/contact-unification-plan.md` §3.
    """
    account_id = _account_id(ctx)

    conf = await conf_crud.get_or_create(account_id=account_id)
    unified = contact_scope.unified(conf)

    if unified:
        # ⚠️ `contact_id` IS A PLATFORM CONTACT ID ON THIS PATH. That is the
        # one id space (plan §3), and it is why the route's parameter name did
        # not change: the client sends back whatever `id` the list gave it,
        # and under this flag that id is the person's.
        #
        # ⚠️ A MISSING PROFILE IS NOT A 404. The person may be in the
        # marketing audience with nobody here having worked them yet — that is
        # a real, renderable record with empty relationship fields, and it is
        # how somebody opens a contact in order to START working them. Only an
        # unresolvable PERSON is a 404.
        person = await foreign.contact_by_id(crud.db_session, account_id,
                                             contact_id)
        if not person:
            return _fail("That contact no longer exists.")
        profile = await profiles.for_contact(account_id=account_id,
                                             contact_id=contact_id)
        owner_index = await _owner_index_for(profiles,
                                             [profile] if profile else [])
        entity_id = contact_id
    else:
        contact = await crud.get_scoped(account_id, contact_id)
        if not contact:
            return _fail("That contact no longer exists.")
        (owner_index,) = await _lookup_indexes(crud, [contact])
        entity_id = contact.id

    # ⚠️ THE ASSOCIATION SIDE FOLLOWS THE PATH. `crm_contact` rows point at
    # `crm_contacts`; `contact` rows point at the platform collection. Phase 6
    # converges every writer on `contact`, after which this branch collapses —
    # until then a unified read that asked for `crm_contact` would find the
    # old rows under a new id and return somebody else's deals.
    assoc_type = assoc_rules.person_entity(unified)
    deal_ids = await assoc.counterparts(
        account_id=account_id, entity_type=assoc_type, entity_id=entity_id,
        want_type="deal")
    company_ids = await assoc.counterparts(
        account_id=account_id, entity_type=assoc_type, entity_id=entity_id,
        want_type="company")

    deal_rows = await deals.list_by(
        {"created_by": account_id, "_id": {"$in": deal_ids[:LINKED_PREVIEW]}},
        limit=LINKED_PREVIEW,
    ) if deal_ids else []

    # ⚠️ THE PRIMARY COMPANY IS UNIONED IN, NOT ASSUMED TO BE ASSOCIATED.
    # `primary_company_id` is a denormalised field on the contact and is set
    # by auto-linking and by the contact form; neither writes an association
    # row. Without this a contact that shows a company in its header would
    # show an empty Companies tab directly beneath it.
    primary_id = getattr(profile if unified else contact,
                         "primary_company_id", None)
    if primary_id and not any(str(c) == str(primary_id) for c in company_ids):
        company_ids = [primary_id, *company_ids]

    company_rows = await companies.list_by(
        {"created_by": companies.oid(account_id),
         "_id": {"$in": companies.oids(company_ids[:LINKED_PREVIEW])}},
        limit=LINKED_PREVIEW,
    ) if company_ids else []

    build = (
        (lambda **kw: _unified_details(person, profile, **kw)) if unified
        else (lambda **kw: _details(contact, **kw))
    )
    return _ok(build(
        owner_index=owner_index,
        linked_deals=sz.linked_collection(
            deal_rows, len(deal_ids),
            lambda d: {"id": d.id, "name": d.name, "amount": d.amount,
                       "status": getattr(d.status, "value", d.status),
                       "stage_id": d.stage_id,
                       "pipeline_id": getattr(d, "pipeline_id", None),
                       "expected_close_date": d.expected_close_date},
        ),
        linked_companies=sz.linked_collection(
            company_rows, len(company_ids),
            lambda c: {"id": c.id, "name": c.name,
                       "domain": getattr(c, "domain", None),
                       "is_primary": str(getattr(c, "id", "")) == str(primary_id or ""),
                       "deal_count": int(getattr(c, "deal_count", 0) or 0)},
        ),
    ))


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
    profiles: CrmContactProfileCrud = Depends(get_crud(CrmContactProfileCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Update a contact.

    ⚠️ `exclude_unset`, not a None check: clearing the job title or detaching
    the company is a real edit, and a None check would read it as "unchanged"
    so the button would silently do nothing.

    On the unified path one PATCH body is split in two — person fields to the
    host, relationship fields to the profile. See `contact_rules.split_updates`.
    """
    account_id = _account_id(ctx)

    conf = await conf_crud.get_or_create(account_id=account_id)
    if contact_scope.unified(conf):
        person = await foreign.contact_by_id(crud.db_session, account_id,
                                             contact_id)
        if not person:
            return _fail("That contact no longer exists.")

        changes = payload.model_dump(exclude_unset=True)
        typed, custom, route_error = await _route_values(
            attributes, account_id, changes.pop("custom_fields", None))
        if route_error:
            return _fail(route_error)
        for field, value in (typed or {}).items():
            changes[field] = value
        if custom:
            changes["custom_fields"] = custom

        person_fields, profile_fields = cr.split_updates(changes)

        if person_fields:
            # ⚠️ FALSE IS NOT "NOTHING CHANGED", IT IS "YOU MAY NOT". The row
            # may belong to another account that shares it through
            # `owner_ids`; `contact_scope.may_write` is the rule and the
            # adapter enforces it in the update filter. Reporting it is the
            # point — a silent success would show the old name back on the
            # next render and look like a caching bug.
            wrote = await host_writes.update_contact(
                crud.db_session, account_id, contact_id, person_fields)
            if not wrote:
                return _fail(
                    "Those details belong to a contact record this account "
                    "does not own, so they were not changed. Your own CRM "
                    "fields can still be edited.")
            person = await foreign.contact_by_id(crud.db_session, account_id,
                                                 contact_id) or person

        # ⚠️ LAZY CREATION HAPPENS ON EDIT. Setting an owner on a marketing
        # contact nobody has worked is exactly how somebody starts working
        # them, so the profile is created here rather than refused.
        profile = await profiles.ensure(
            account_id=account_id, contact_id=contact_id,
            actor_user_id=ctx.user.id)
        if profile is None:
            return _fail("Could not save your changes to that contact.")

        if profile_fields or person_fields:
            patch = dict(profile_fields)
            # Keep the identity cache in step with the edit that just landed,
            # so the list does not show the old name until the nightly
            # reconcile. Same three fields, same source of truth.
            patch.update({
                "full_name": cr.display_name(person.get("first_name"),
                                             person.get("last_name"),
                                             person.get("email")),
                "email": person.get("email"),
                "job_title": person.get("job_title"),
            })
            await profiles.update_scoped(account_id, profile.id, patch)
            profile = await profiles.for_contact(account_id=account_id,
                                                 contact_id=contact_id)

        owner_index = await _owner_index_for(profiles, [profile] if profile else [])
        return _ok(_unified_details(person, profile, owner_index=owner_index),
                   "Contact saved.")

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
    profiles: CrmContactProfileCrud = Depends(get_crud(CrmContactProfileCrud)),
    conf_crud: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Delete permanently. Account owner or `crm:manage_others` only.

    ⚠️ ARCHIVING WAS REMOVED ON 29 OCT 2026, so there is no recoverable
    outcome left and `hard` is vestigial — accepted, ignored. The owner gate
    used to guard only the permanent half; it now guards the whole operation.

    ⚠️ A HARD DELETE DOES NOT TOUCH THE PLATFORM CONTACT. If this person was
    added to the marketing audience they are a subscriber with their own
    consent record, and deleting a CRM row is not consent to remove them from
    a mailing list — that is a different decision, made on that screen.
    """
    account_id = _account_id(ctx)

    conf = await conf_crud.get_or_create(account_id=account_id)
    if contact_scope.unified(conf):
        # ⚠️ ARCHIVES THE PROFILE AND NOTHING ELSE — decision 0.4. The person
        # stays in the marketing audience with their consent untouched: this
        # account is saying it has stopped working them, not that they asked
        # to stop being emailed. Those are different decisions made by
        # different people on different screens, and conflating them is how a
        # salesperson tidying their list unsubscribes a customer.
        #
        # ⚠️ AND `hard` NO LONGER MEANS "DELETE THE PERSON". On the legacy
        # path it destroyed a `crm_contacts` row this service owned. Here the
        # only thing this account owns is the relationship, so a hard delete
        # removes the profile and stops there.
        profile = await profiles.for_contact(account_id=account_id,
                                             contact_id=contact_id)
        if profile is None:
            return _fail("You are not working that contact.")
        name = profile.full_name or "Contact"

        if not ctx.is_owner and not ctx.has(perms.MANAGE_OTHERS):
            return _fail("Only an account owner can delete a contact. "
                         "Deleting is permanent — there is no archive to "
                         "restore from.")

        await assoc.delete_all_for(account_id=account_id,
                                   entity_type="contact",
                                   entity_id=contact_id)
        ok = await profiles.delete_scoped(account_id, profile.id)
        if not ok:
            return _fail("Could not delete that contact.")
        message = f"{name} removed from your CRM."

        await ar.record(activity.write, ar.build_activity(
            account_id=account_id, entity_type="contact",
            entity_id=contact_id,
            activity_type="contact_deleted",
            title=name, actor_user_id=ctx.user.id,
            actor_name=_actor_name(ctx)))
        return _ok({"id": str(contact_id)}, message
                   + " They remain in your marketing audience.")

    contact = await crud.get_scoped(account_id, contact_id)
    if not contact:
        return _fail("That contact no longer exists.")
    if not cr.may_write(contact, **_actor(ctx)):
        return _fail(cr.write_refusal())
    # Always permanent, owner-gated — see the note in the unified branch above.
    if not ctx.is_owner and not ctx.has(perms.MANAGE_OTHERS):
        return _fail("Only an account owner can delete a contact. Deleting is "
                     "permanent — there is no archive to restore from.")

    name = contact.full_name
    await assoc.delete_all_for(account_id=account_id, entity_type=OBJECT,
                               entity_id=contact.id)
    ok = await crud.delete_scoped(account_id, contact_id)
    if not ok:
        return _fail("Could not delete that contact.")
    message = f"{name} deleted permanently."

    await ar.record(activity.write, ar.build_activity(
        account_id=account_id, entity_type=OBJECT, entity_id=contact_id,
        activity_type="deleted", title=message,
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))
    return _ok({"id": str(contact_id)}, message)


# ── no marketing link ────────────────────────────────────────────────────────
# `POST /{id}/link` and `DELETE /{id}/link` lived here (CRM-322). They are
# gone, with `contact_link_service` and the `contact_id` field: the CRM reads
# and writes `crm_contacts` and nothing else. See the model docstring for why,
# and `docs/CRM/api-contract.md` for what a client should do instead.

# ── promotion ────────────────────────────────────────────────────────────────
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
