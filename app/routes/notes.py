"""CRM notes API. CRM-214 / CRM-215 / CRM-217.

Mounted at ``/api/v1/crm/notes``::

    GET    ""              notes on one record
    POST   ""              create
    PATCH  "/{id}"         edit the body
    DELETE "/{id}"         delete
    POST   "/{id}/pin"     · "/{id}/unpin"

PERMISSIONS INHERIT THE PARENT. A note on a deal needs ``deals:read`` to see
and ``deals:write`` to write; a note on a company needs the company pair. There
is no ``notes:*`` permission, because a note is not a thing you have access to
independently of what it is attached to — granting someone deal access and then
separately having to grant note access would be a trap, and forgetting to would
silently hide half the deal's history.

``require_any`` gates the route so a role holding only one of the two pairs can
still reach it; the handler then checks the permission for the *specific*
parent. Both layers are needed: the route-level check keeps a user with no CRM
access out entirely, the handler-level one stops a companies-only role reading
a deal's notes.

BODY IS SANITISED ON WRITE, never on read — see
``api/services/engage/crm/sanitise.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.crud import (
    CompanyCrud, CrmActivityCrud, CrmNoteCrud, DealCrud,
)
from app.models.note import CrmNoteModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require_any
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.note import (
    NoteCreateRequest, NoteDetails, NoteListResponse, NoteUpdateRequest,
)
from app.services import (
    activity_recorder as ar, sanitise, serializers as sz,
)

router = APIRouter()

# The parent kinds a note can hang off, and the permissions each implies.
PARENTS = {
    "deal": ("deals:read", "deals:write"),
    "company": ("companies:read", "companies:write"),
    "contact": ("contacts:read", "contacts:write"),
}


def _account_id(ctx: RequestContext) -> PyObjectId:
    return PyObjectId(ctx.account_id) if ctx.account_id else ctx.user.id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fail(message: str, data: Any = None) -> ResponseSchema:
    return ResponseSchema(status="failure", message=message, data=data)


def _actor_name(ctx: RequestContext) -> Optional[str]:
    u = ctx.user
    name = f"{getattr(u,'first_name','') or ''} {getattr(u,'last_name','') or ''}".strip()
    return name or getattr(u, "email", None)


def _may(ctx: RequestContext, entity_type: str, write: bool) -> bool:
    """Does the caller hold the permission this note's parent implies?"""
    pair = PARENTS.get(entity_type)
    if not pair:
        return False
    return ctx.has(pair[1] if write else pair[0])


def _parents_of(note: CrmNoteModel) -> List[str]:
    kinds = []
    if note.deal_ids:
        kinds.append("deal")
    if note.company_ids:
        kinds.append("company")
    if note.contact_ids:
        kinds.append("contact")
    return kinds


def _may_touch(ctx: RequestContext, note: CrmNoteModel, *, write: bool) -> bool:
    """A note attached to several records needs access to at least one.

    Requiring ALL of them would hide a note from someone who can legitimately
    see the deal it is on, just because it also mentions a contact they cannot.
    """
    kinds = _parents_of(note)
    if not kinds:
        return True          # orphaned note: nothing to inherit from
    return any(_may(ctx, kind, write) for kind in kinds)


def _details(note: CrmNoteModel) -> NoteDetails:
    payload = note.model_dump(by_alias=True)
    payload["preview"] = sanitise.plain_text(note.body, limit=140)
    return NoteDetails.model_validate(payload)


# ── read ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=NoteListResponse)
async def list_notes(
    deal_id: Optional[PyObjectId] = Query(None),
    company_id: Optional[PyObjectId] = Query(None),
    contact_id: Optional[PyObjectId] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    ctx: RequestContext = Depends(
        require_any("companies:read", "deals:read", "contacts:read")),
    crud: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
):
    """Notes on one record. Pinned first, then newest first."""
    account_id = _account_id(ctx)

    target = (("deal", deal_id) if deal_id else
              ("company", company_id) if company_id else
              ("contact", contact_id) if contact_id else (None, None))
    if not target[1]:
        return NoteListResponse(data=[], total=0, status="failure",
                                message="Say which record's notes you want.")
    if not _may(ctx, target[0], write=False):
        return NoteListResponse(data=[], total=0, status="failure",
                                message="You don't have access to that record.")

    rows = await crud.list_for(account_id=account_id, entity_type=target[0],
                               entity_id=target[1], skip=skip, limit=limit)
    total = await crud.count_for(account_id=account_id, entity_type=target[0],
                                 entity_id=target[1])
    return NoteListResponse(data=[_details(r) for r in rows], total=total,
                            status="success")


# ── write ────────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[NoteDetails])
async def create_note(
    payload: NoteCreateRequest,
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)

    if not (payload.deal_ids or payload.company_ids or payload.contact_ids):
        return _fail("A note has to be attached to a record.")

    body = sanitise.sanitise_html(payload.body)
    if sanitise.is_empty(body):
        # `<p><br></p>` is what an empty editor submits. Truthiness on the raw
        # string would save it as a note.
        return _fail("Write something first.")

    for kind, ids in (("deal", payload.deal_ids), ("company", payload.company_ids),
                      ("contact", payload.contact_ids)):
        if ids and not _may(ctx, kind, write=True):
            return _fail(f"You don't have permission to add notes to a {kind}.")

    note = CrmNoteModel(
        created_by=account_id,
        body=body,
        is_pinned=bool(payload.is_pinned),
        deal_ids=payload.deal_ids or [],
        company_ids=payload.company_ids or [],
        contact_ids=payload.contact_ids or [],
        author_id=ctx.user.id,
        author_name=_actor_name(ctx),
    )
    created = await crud.insert(note)
    if not created:
        return _fail("Could not save that note.")

    await _record_note_activity(activity, ctx, created, "note_added")
    return ResponseSchema(status="success", message="Note added.",
                          data=_details(created))


@router.patch("/{note_id}", response_model=ResponseSchema[NoteDetails])
async def update_note(
    payload: NoteUpdateRequest,
    note_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Edit a note's body. Attachments are fixed at creation.

    Only the author may edit — a note is a person's own words, and someone else
    silently rewriting them under that name is worse than not being able to fix
    a typo. Anyone with write access may still delete it.
    """
    account_id = _account_id(ctx)
    note = await crud.get_scoped(account_id, note_id)
    if not note:
        return _fail("That note no longer exists.")
    if not _may_touch(ctx, note, write=True):
        return _fail("You don't have permission to edit that note.")
    if str(note.author_id) != str(ctx.user.id) and not ctx.is_owner:
        return _fail("You can only edit your own notes.")

    body = sanitise.sanitise_html(payload.body)
    if sanitise.is_empty(body):
        return _fail("A note can't be empty. Delete it instead.")

    note.body = body
    note.updated_at = _utcnow()
    await note.save()

    await _record_note_activity(activity, ctx, note, "note_updated")
    return ResponseSchema(status="success", message="Note updated.",
                          data=_details(note))


@router.delete("/{note_id}", response_model=ResponseSchema)
async def delete_note(
    note_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)
    note = await crud.get_scoped(account_id, note_id)
    if not note:
        return _fail("That note no longer exists.")
    if not _may_touch(ctx, note, write=True):
        return _fail("You don't have permission to delete that note.")

    await _record_note_activity(activity, ctx, note, "note_deleted")
    await crud.delete_scoped(account_id, note_id)
    return ResponseSchema(status="success", message="Note deleted.", data=None)


@router.post("/{note_id}/pin", response_model=ResponseSchema[NoteDetails])
async def pin_note(
    note_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
):
    return await _set_pinned(ctx, crud, note_id, True)


@router.post("/{note_id}/unpin", response_model=ResponseSchema[NoteDetails])
async def unpin_note(
    note_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
):
    return await _set_pinned(ctx, crud, note_id, False)


async def _set_pinned(ctx, crud, note_id, pinned: bool):
    account_id = _account_id(ctx)
    note = await crud.get_scoped(account_id, note_id)
    if not note:
        return _fail("That note no longer exists.")
    if not _may_touch(ctx, note, write=True):
        return _fail("You don't have permission to change that note.")
    # Pinning is shared state, not per-user: a pinned note is the team's
    # "read this first", which is the point of it.
    note.is_pinned = pinned
    note.updated_at = _utcnow()
    await note.save()
    return ResponseSchema(status="success",
                          message="Note pinned." if pinned else "Note unpinned.",
                          data=_details(note))


# ── activity (CRM-217) ───────────────────────────────────────────────────────
async def _record_note_activity(activity, ctx, note: CrmNoteModel, kind: str) -> None:
    """One activity per attached record, so a note shows on every timeline.

    The rows are BUILT BY ``ar.note_activities`` — the note importer (CRM-413)
    writes the same rows, and two builders for one timeline entry is how the
    two wordings drift apart.
    """
    for payload in ar.note_activities(
        kind=kind,
        note_id=note.id,
        preview=sanitise.plain_text(note.body, limit=140),
        targets=(("deal", note.deal_ids), ("company", note.company_ids),
                 ("contact", note.contact_ids)),
        account_id=_account_id(ctx),
        actor_user_id=ctx.user.id,
        actor_name=_actor_name(ctx),
    ):
        await ar.record(activity.write, payload)
