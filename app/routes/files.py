"""CRM file attachments. CRM-218 / 219 / 220 / 221.

Mounted at ``/api/v1/crm/files``::

    GET    ""                    files on one record
    POST   ""                    multipart upload
    GET    "/{id}/download"      a short-lived signed URL
    DELETE "/{id}"               remove the row AND the object

PERMISSIONS INHERIT THE PARENT, exactly as notes do: a file on a deal needs
``deals:read`` to list and ``deals:write`` to attach. There is no ``files:*``
permission — see ``api/routes/crm/notes.py`` for the reasoning.

THREE THINGS THIS ENDPOINT REFUSES TO DO:

* **Trust the extension.** The type is decided from the file's first bytes
  (``file_rules.validate_upload``). ``invoice.pdf`` containing HTML is refused.
* **Trust the client's Content-Type.** It is not an input at all; the type we
  serve is derived from the sniffed family.
* **Read the whole body before checking its size.** The 10 MB cap is applied
  while streaming, so a 2 GB upload is abandoned after 10 MB rather than after
  the process has already allocated 2 GB for it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile
from fastapi.responses import Response

from app.crud import CrmActivityCrud, CrmFileCrud
from app.models.file import CrmFileModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require_any
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.file import FileDetails, FileListResponse
from app.services import (
    activity_recorder as ar, file_rules as rules, file_storage as storage,
)
from app.services.file_rules import FileRuleError

router = APIRouter()

# How much is read at a time while enforcing the size cap.
CHUNK = 256 * 1024

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
    pair = PARENTS.get(entity_type)
    return bool(pair) and ctx.has(pair[1] if write else pair[0])


def _details(row: CrmFileModel) -> FileDetails:
    payload = row.model_dump(by_alias=True)
    payload["size_label"] = rules.human_size(row.size_bytes)
    return FileDetails.model_validate(payload)


async def _read_capped(upload: UploadFile) -> tuple:
    """Read the upload, stopping the moment it exceeds the cap.

    Returns ``(head, body, error)``. The point of streaming here is that an
    oversized upload costs us 10 MB of memory, not its full size — reading
    first and checking `len()` afterwards would let a 2 GB file be allocated
    before we ever look at it.
    """
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > rules.MAX_BYTES:
            return b"", b"", (
                f"That file is over the {rules.human_size(rules.MAX_BYTES)} limit."
            )
        chunks.append(chunk)
    body = b"".join(chunks)
    return body[: rules.SNIFF_BYTES], body, None


# ── read ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=FileListResponse)
async def list_files(
    deal_id: Optional[PyObjectId] = Query(None),
    company_id: Optional[PyObjectId] = Query(None),
    contact_id: Optional[PyObjectId] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    ctx: RequestContext = Depends(
        require_any("companies:read", "deals:read", "contacts:read")),
    crud: CrmFileCrud = Depends(get_crud(CrmFileCrud)),
):
    account_id = _account_id(ctx)
    kind, entity_id = (("deal", deal_id) if deal_id else
                       ("company", company_id) if company_id else
                       ("contact", contact_id) if contact_id else (None, None))
    if not entity_id:
        return FileListResponse(data=[], total=0, status="failure",
                                message="Say which record's files you want.")
    if not _may(ctx, kind, write=False):
        return FileListResponse(data=[], total=0, status="failure",
                                message="You don't have access to that record.")

    rows = await crud.list_for(account_id=account_id, entity_type=kind,
                               entity_id=entity_id, skip=skip, limit=limit)
    total = await crud.count_for(account_id=account_id, entity_type=kind,
                                 entity_id=entity_id)
    return FileListResponse(data=[_details(r) for r in rows], total=total,
                            status="success")


# ── upload ───────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[FileDetails])
async def upload_file(
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    entity_id: str = Form(...),
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmFileCrud = Depends(get_crud(CrmFileCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)

    if entity_type not in PARENTS:
        return _fail("Files attach to a lead, a company or a contact.")
    if not _may(ctx, entity_type, write=True):
        return _fail(f"You don't have permission to attach files to a {entity_type}.")

    head, body, error = await _read_capped(file)
    if error:
        return _fail(error)

    try:
        # The bytes decide, not the name and not the client's Content-Type.
        ext, content_type = rules.validate_upload(file.filename, head, size=len(body))
    except FileRuleError as ex:
        return _fail(str(ex))

    display_name = rules.safe_filename(file.filename)
    key = rules.storage_key(account_id, entity_type, ext)

    stored, store_error = storage.put(key, body, content_type)
    if not stored:
        return _fail(store_error or "Could not store that file.")

    row = CrmFileModel(
        created_by=account_id,
        storage_key=key,
        filename=display_name,
        content_type=content_type,
        size_bytes=len(body),
        entity_type=entity_type,
        entity_id=PyObjectId(entity_id),
        uploaded_by_id=ctx.user.id,
        uploaded_by_name=_actor_name(ctx),
    )
    created = await crud.insert(row)
    if not created:
        # The object is already in the bucket; without the row nothing points
        # at it, so remove it rather than leave an orphan behind.
        storage.delete(key)
        return _fail("Could not save that file.")

    await ar.record(activity.write, ar.build_activity(
        account_id=account_id, entity_type=entity_type,
        entity_id=PyObjectId(entity_id), activity_type="file_uploaded",
        title=f"File attached: {display_name}",
        metadata={"file_id": str(created.id), "size": len(body)},
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    return ResponseSchema(status="success", message="File attached.",
                          data=_details(created))


# ── download ─────────────────────────────────────────────────────────────────
@router.get("/{file_id}/download", response_model=ResponseSchema)
async def download_file(
    file_id: PyObjectId = Path(...),
    disposition: str = Query(
        "attachment", pattern="^(attachment|inline)$",
        description="`attachment` saves the file; `inline` renders it in the "
                    "browser, for the preview. Refused for families a browser "
                    "cannot show.",
    ),
    ctx: RequestContext = Depends(
        require_any("companies:read", "deals:read", "contacts:read")),
    crud: CrmFileCrud = Depends(get_crud(CrmFileCrud)),
):
    """Mint a short-lived signed URL, to save or to render.

    Tenancy and the parent-record permission are both checked BEFORE signing.
    A signed URL is a bearer token for the object — once minted it works for
    anyone holding it, so the check cannot happen afterwards.

    ⚠️ `inline` IS REFUSED FOR WHAT A BROWSER CANNOT SHOW (zip, Office), and
    that is a real refusal rather than a courtesy: minting an inline URL for a
    .docx produces a link whose behaviour depends on the viewer's plugins —
    usually a download, sometimes binary soup in a tab — so the preview button
    would be a button that does something different on every machine.

    The reason `inline` is safe at all is upstream: SVG and HTML are refused at
    upload by extension AND by magic bytes, so no stored object can execute
    script when rendered. See `file_rules.PREVIEWABLE_FAMILIES`.
    """
    account_id = _account_id(ctx)
    row = await crud.get_scoped(account_id, file_id)
    if not row:
        return _fail("That file no longer exists.")
    if not _may(ctx, row.entity_type, write=False):
        return _fail("You don't have access to that file.")
    if disposition == "inline" and not rules.previewable(row.filename):
        return _fail(rules.preview_refusal(row.filename))

    url = storage.signed_url(row.storage_key, row.filename, row.content_type,
                             disposition=disposition)
    if not url:
        return _fail("Could not prepare that download. Try again.")

    return ResponseSchema(status="success", message="", data={
        "url": url,
        "filename": row.filename,
        "content_type": row.content_type,
        "disposition": disposition,
        "expires_in": storage.SIGNED_URL_TTL_SECONDS,
    })


@router.get("/local/{key:path}")
async def download_local(
    key: str = Path(...),
    disposition: str = Query("attachment", pattern="^(attachment|inline)$"),
    ctx: RequestContext = Depends(
        require_any("companies:read", "deals:read", "contacts:read")),
    crud: CrmFileCrud = Depends(get_crud(CrmFileCrud)),
):
    """Dev-only: serve the bytes when there is no bucket to sign against.

    Still tenancy-checked. The key is looked up as a *record owned by this
    account* rather than read off disk directly — otherwise this would be an
    arbitrary-file-read endpoint wearing a CRM hat.
    """
    account_id = _account_id(ctx)
    row = await crud.by_key(account_id=account_id, storage_key=key)
    if not row:
        return Response(status_code=404)
    if not _may(ctx, row.entity_type, write=False):
        return Response(status_code=404)

    body = storage.read_local(key)
    if body is None:
        return Response(status_code=404)
    inline = disposition == "inline" and rules.previewable(row.filename)
    return Response(
        content=body,
        media_type=row.content_type,
        headers={
            "Content-Disposition":
                f'{"inline" if inline else "attachment"}; '
                f'filename="{row.filename}"',
            # ⚠️ REQUIRED ON THIS ROUTE, not optional hardening. These bytes
            # are served from the API's OWN ORIGIN — unlike the S3 path, where
            # the bucket is a different origin — so a .txt whose contents start
            # with `<html>` being content-sniffed as HTML would execute in the
            # app's origin with the user's session. `nosniff` makes the browser
            # honour `text/plain`, which is what the type says.
            "X-Content-Type-Options": "nosniff",
        },
    )


# ── delete (CRM-221) ─────────────────────────────────────────────────────────
@router.delete("/{file_id}", response_model=ResponseSchema)
async def delete_file(
    file_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(
        require_any("companies:write", "deals:write", "contacts:write")),
    crud: CrmFileCrud = Depends(get_crud(CrmFileCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Delete the row and the stored object.

    The ROW goes first. If the object delete fails the file is already
    invisible and unreachable, and the orphan sweep will collect it later —
    whereas deleting the object first and then failing to delete the row leaves
    a listing entry whose download is permanently broken.
    """
    account_id = _account_id(ctx)
    row = await crud.get_scoped(account_id, file_id)
    if not row:
        return _fail("That file no longer exists.")
    if not _may(ctx, row.entity_type, write=True):
        return _fail("You don't have permission to delete that file.")

    key, name, entity_type, entity_id = (
        row.storage_key, row.filename, row.entity_type, row.entity_id)
    await crud.delete_scoped(account_id, file_id)
    storage.delete(key)

    await ar.record(activity.write, ar.build_activity(
        account_id=account_id, entity_type=entity_type, entity_id=entity_id,
        activity_type="file_deleted", title=f"File removed: {name}",
        metadata={"filename": name},
        actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))

    return ResponseSchema(status="success", message="File deleted.", data=None)
