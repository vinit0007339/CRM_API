"""CRM import API. CRM-229 / CRM-230 / CRM-231.

Mounted at ``/api/v1/crm/imports``::

    POST   "/preview"      multipart; parse, sniff, suggest a mapping
    POST   ""              multipart + mapping + options; starts a background run
    GET    ""              recent imports
    GET    "/{id}"         poll progress
    POST   "/{id}/cancel"
    GET    "/{id}/errors"  CSV of rejected rows, with their original row numbers

SCOPE: COMPANIES, DEALS AND NOTES.

⚠️ CONTACT IMPORT IS GONE, and this is the second time this docstring's scope
line has changed — it claimed "companies and deals only" for a release after
contact import shipped, then listed contacts, and now does not again. A scope
note nobody updates is worse than none, so: the CRM has no people object. To
import people, import them into the marketing audience (the `/lists` import),
where they get their own consent record.

A note's `contact_id` / `contact_email` columns resolve against the PLATFORM
``contacts`` collection, account-scoped — the same thing an association's
contact side points at.

THE FILE IS PARSED IN THE REQUEST, NOT IN THE BACKGROUND. A file we cannot read
is the single most common failure, and "your import failed, check back in a
minute" is a much worse way to learn that you uploaded the wrong file than
being told immediately. Only the row-by-row work is deferred.

The consequence is that the parsed rows live in memory for the life of the job,
which is what ``MAX_IMPORT_BYTES`` and ``MAX_ROWS`` bound.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, Path, Query, UploadFile,
)
from fastapi.responses import Response

from app.crud import (
    CompanyCrud, CrmActivityCrud, CrmAssociationCrud, CrmAttributeCrud,
    CrmImportCrud, CrmNoteCrud, DealCrud, PipelineCrud,
)
from app.models.import_run import CrmImportModel
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require_any
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.import_run import (
    ImportColumn, ImportDetails, ImportListResponse, ImportOptions, ImportPreview,
)
from app.services import import_rules as ir
from app.services import import_worker as iw
from app.services.import_worker import ImportRunner

router = APIRouter()

# Which permission each import type needs. An import is a bulk create, so it
# needs the same write permission a single create does — nothing weaker.
#
# ⚠️ EACH VALUE IS A TUPLE AND THE CHECK IS "HOLDS ANY OF" (CRM-413). A note
# has no permission of its own — it inherits its parent's, exactly as
# `notes.py::PARENTS` does — so any write grant opens the door and the WORKER
# decides each row against the parent it actually names. A single string here
# would have forced one arbitrary grant to stand for all three.
ENTITY_PERMISSIONS = {
    "company": ("companies:write",),
    "deal": ("deals:write",),
    "note": ("companies:write", "deals:write", "contacts:write"),
}

# Which parent kind each note permission unlocks. The inverse of the `note`
# row above, and the per-row gate the worker is given.
NOTE_PARENT_PERMISSIONS = {"company": "companies:write", "deal": "deals:write",
                           "contact": "contacts:write"}

# The column an import of each type cannot do without: one rule per entity, as
# ``(any_of, message)``.
#
# ⚠️ ONE ENTRY PER ENTITY, not a single `name` check for everything. The old
# spelling demanded `name` for every type, and when a type arrived whose
# identity column was not `name` (contacts, since removed) EVERY import of it
# was refused at the door with "Map a column to Name" — a message naming a
# field its map screen did not offer. The worker had 32 tests; the route it is
# reached through had none. Hence the route-level tests in
# `test_e29_note_import.py`.
REQUIRED_COLUMNS = {
    "company": ((("name",), "Map a column to Name — every company needs one."),),
    "deal": ((("name",), "Map a column to Name — every deal needs one."),),
    "note": ((("body",),
              "Map a column to Note — there is nothing to import otherwise."),
             (tuple(field for field, _ in iw.NOTE_TARGET_FIELDS),
              "Map a column to a company, deal or contact — a note has to be "
              "attached to a record.")),
}

MAX_ERROR_SAMPLES = 10

# ⚠️ `require_any`, NOT `require("companies:write")`. The route-level gate's
# job is to keep a caller with no CRM write access out entirely; deciding WHICH
# type may be imported is `_may_import` below, per request. Demanding
# `companies:write` here meant a deals-only or contacts-only role could not
# reach an import of its own records at all — a hole the per-entity check could
# not see, because the dependency refuses the request before the handler runs.
WRITE = require_any("companies:write", "deals:write", "contacts:write")
READ = require_any("companies:read", "deals:read", "contacts:read")


def _may_import(ctx: RequestContext, entity_type: str) -> bool:
    """Does the caller hold a permission this import type accepts?"""
    return any(ctx.has(permission)
               for permission in ENTITY_PERMISSIONS.get(entity_type, ()))


def _note_parents(ctx: RequestContext) -> List[str]:
    """The parent kinds this caller may attach imported notes to."""
    return [kind for kind, permission in NOTE_PARENT_PERMISSIONS.items()
            if ctx.has(permission)]


def _missing_column(entity_type: str, mapping: List[Any]) -> Optional[str]:
    """The message for the first required column the mapping lacks, if any.

    Checked up front rather than per row: finding out row by row would produce
    one identical error per row in a report of nine thousand.
    """
    mapped = {target for target in mapping if target}
    for any_of, message in REQUIRED_COLUMNS.get(entity_type, ()):
        if not mapped.intersection(any_of):
            return message
    return None


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


async def _read_upload(file: UploadFile) -> bytes:
    """Read the whole upload, refusing anything past the cap.

    Read in chunks and stop at the limit rather than trusting a declared
    content-length: the header is the client's claim, and the bytes are the
    only thing that actually arrives.
    """
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > ir.MAX_IMPORT_BYTES:
            raise ValueError(
                f"That file is over the "
                f"{ir.MAX_IMPORT_BYTES // (1024 * 1024)} MB limit. "
                f"Split it and import in parts.")
        chunks.append(chunk)
    return b"".join(chunks)


def _details(row: CrmImportModel) -> ImportDetails:
    payload = row.model_dump(by_alias=True)
    total = row.total_rows or 0
    done = row.processed_rows or 0
    # Computed here, not in the browser: two implementations of "how far along"
    # is how a bar reaches 100% while the job is still running.
    payload["percent"] = 100 if row.status in ("completed", "completed_with_errors",
                                               "failed", "cancelled") else (
        min(99, int(done * 100 / total)) if total else 0)
    payload["is_finished"] = row.status in ("completed", "completed_with_errors",
                                            "failed", "cancelled")
    payload["error_count"] = row.failed_count or 0
    payload["error_samples"] = (row.errors or [])[:MAX_ERROR_SAMPLES]
    return ImportDetails.model_validate(payload)


def _parse_json_field(raw: Optional[str], field: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not valid JSON.") from exc


# ── preview ──────────────────────────────────────────────────────────────────
@router.post("/preview", response_model=ResponseSchema[ImportPreview])
async def preview_import(
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    ctx: RequestContext = Depends(WRITE),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
):
    """Parse the first rows, sniff the format, suggest a mapping.

    Gated on any CRM write at the route and re-checked per entity below — a
    preview reads no records, but it is the front door to a bulk write and
    should not be open to a role that cannot perform one.
    """
    account_id = _account_id(ctx)
    if entity_type not in ENTITY_PERMISSIONS:
        return _fail("You can import companies, deals or notes.")
    if not _may_import(ctx, entity_type):
        return _fail(f"You don't have permission to import {entity_type}s.")

    try:
        raw = await _read_upload(file)
    except ValueError as ex:
        return _fail(str(ex))
    if not raw:
        return _fail("That file is empty.")

    try:
        # One extra row so "there is more than this" can be stated honestly
        # rather than inferred from a full preview.
        headers, rows, meta = ir.read_rows(raw, limit=ir.PREVIEW_ROWS + 1)
    except ValueError as ex:
        return _fail(str(ex))
    except Exception:  # pylint: disable=broad-except
        return _fail("That file couldn't be read. CSV and XLSX are supported.")

    if not headers:
        return _fail("That file has no header row. The first row should name "
                     "the columns.")

    # Notes have no attribute definitions to suggest from — `crm_attributes`
    # has no `note` object type — so they bring their own static catalogue and
    # their own synonym table. Without it every column of a note file would
    # come back unmapped and the user would fill five dropdowns by hand.
    if entity_type == "note":
        definitions, synonyms = ir.NOTE_COLUMNS, ir.NOTE_SYNONYMS
    else:
        definitions = await attributes.list(account_id=account_id,
                                            object_type=entity_type)
        synonyms = None
    suggested = ir.suggest_mapping(headers, definitions, synonyms)
    samples = ir.column_samples(headers, rows[:ir.PREVIEW_ROWS])

    preview = ImportPreview(
        entity_type=entity_type,
        file_name=file.filename,
        file_size=len(raw),
        file_format=meta.get("format"),
        encoding=meta.get("encoding"),
        delimiter=meta.get("delimiter"),
        columns=[
            ImportColumn(index=index, header=header,
                         samples=samples[index] if index < len(samples) else [],
                         suggested=suggested[index] if index < len(suggested) else None)
            for index, header in enumerate(headers)
        ],
        rows=rows[:ir.PREVIEW_ROWS],
        total_preview_rows=min(len(rows), ir.PREVIEW_ROWS),
        truncated=len(rows) > ir.PREVIEW_ROWS,
    )
    return ResponseSchema(status="success", message="", data=preview)


# ── run ──────────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[ImportDetails])
async def start_import(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    mapping: str = Form(..., description="JSON array, one entry per column, "
                                         "null for columns to skip"),
    options: Optional[str] = Form(None, description="JSON object"),
    ctx: RequestContext = Depends(WRITE),
    imports: CrmImportCrud = Depends(get_crud(CrmImportCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    attributes: CrmAttributeCrud = Depends(get_crud(CrmAttributeCrud)),
    associations: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
    notes: CrmNoteCrud = Depends(get_crud(CrmNoteCrud)),
):
    """Start an import. Returns immediately; poll ``GET /{id}`` for progress."""
    account_id = _account_id(ctx)
    if entity_type not in ENTITY_PERMISSIONS:
        return _fail("You can import companies, deals or notes.")
    if not _may_import(ctx, entity_type):
        return _fail(f"You don't have permission to import {entity_type}s.")

    try:
        parsed_mapping = _parse_json_field(mapping, "mapping")
        parsed_options = _parse_json_field(options, "options") or {}
    except ValueError as ex:
        return _fail(str(ex))

    if not isinstance(parsed_mapping, list) or not any(parsed_mapping):
        return _fail("Map at least one column before importing.")
    missing = _missing_column(entity_type, parsed_mapping)
    if missing:
        return _fail(missing)

    try:
        validated = ImportOptions.model_validate(parsed_options)
    except Exception as ex:  # pylint: disable=broad-except
        return _fail(_first_message(ex))

    if entity_type == "note" and validated.update_existing:
        # A note import only ever ADDS. There is no key that identifies "the
        # same note" — two identical call summaries a week apart are two
        # notes — so `update_existing` has nothing to match on, and silently
        # ignoring it would leave the user believing a re-import had edited
        # notes in place rather than doubling them.
        return _fail("Notes can only be added by an import, not updated. "
                     "Turn off 'update existing'.")

    try:
        raw = await _read_upload(file)
    except ValueError as ex:
        return _fail(str(ex))
    if not raw:
        return _fail("That file is empty.")

    try:
        headers, rows, meta = ir.read_rows(raw, limit=ir.MAX_ROWS + 1)
    except ValueError as ex:
        return _fail(str(ex))
    except Exception:  # pylint: disable=broad-except
        return _fail("That file couldn't be read. CSV and XLSX are supported.")

    if len(rows) > ir.MAX_ROWS:
        return _fail(f"That file has more than {ir.MAX_ROWS:,} rows. "
                     f"Split it and import in parts.")
    if len(parsed_mapping) != len(headers):
        # A mapping built against a different file would silently write the
        # wrong column into every field.
        return _fail("The mapping doesn't match this file's columns. "
                     "Preview it again.")

    run = CrmImportModel(
        created_by=account_id,
        entity_type=entity_type,
        file_name=file.filename,
        file_size=len(raw),
        file_format=meta.get("format"),
        encoding=meta.get("encoding"),
        delimiter=meta.get("delimiter"),
        headers=headers,
        mapping=parsed_mapping,
        options=validated.model_dump(mode="json"),
        status="pending",
        total_rows=len(list(ir.iter_data_rows(rows))),
        created_by_user_id=ctx.user.id,
        created_by_name=_actor_name(ctx),
    )
    created = await imports.insert(run)
    if not created:
        return _fail("Could not start that import.")

    runner = ImportRunner(
        import_id=created.id, account_id=account_id, entity_type=entity_type,
        headers=headers, rows=rows, mapping=parsed_mapping,
        options=validated.model_dump(), actor_user_id=ctx.user.id,
        actor_name=_actor_name(ctx), companies=companies,
        deals=deals, pipelines=pipelines, attributes=attributes,
        associations=associations, activity=activity, imports=imports,
        notes=notes, allowed_note_parents=_note_parents(ctx),
    )
    background.add_task(runner.run)

    return ResponseSchema(status="success", message="Import started.",
                          data=_details(created))


def _first_message(ex: Exception) -> str:
    """The first validation message, without pydantic's punctuation."""
    errors = getattr(ex, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            return str(first.get("msg", "")).replace("Value error, ", "") or str(ex)
        except Exception:  # pylint: disable=broad-except
            pass
    return str(ex)


# ── progress ─────────────────────────────────────────────────────────────────
@router.get("", response_model=ImportListResponse)
async def list_imports(
    entity_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    ctx: RequestContext = Depends(READ),
    imports: CrmImportCrud = Depends(get_crud(CrmImportCrud)),
):
    account_id = _account_id(ctx)
    filters = imports.build_filter(account_id=account_id, entity_type=entity_type)
    rows = await imports.list_by(filters, sort=[("created_at", -1)],
                                 skip=skip, limit=limit)
    total = await imports.count_by(filters)
    return ImportListResponse(data=[_details(r) for r in rows], total=total,
                              status="success")


@router.get("/{import_id}", response_model=ResponseSchema[ImportDetails])
async def get_import(
    import_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(READ),
    imports: CrmImportCrud = Depends(get_crud(CrmImportCrud)),
):
    row = await imports.get_scoped(_account_id(ctx), import_id)
    if row is None:
        return _fail("That import no longer exists.")
    return ResponseSchema(status="success", message="", data=_details(row))


@router.post("/{import_id}/cancel", response_model=ResponseSchema[ImportDetails])
async def cancel_import(
    import_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(WRITE),
    imports: CrmImportCrud = Depends(get_crud(CrmImportCrud)),
):
    """Ask a running import to stop.

    Sets a flag; the worker reads it between batches. It does NOT roll back —
    an import is not a transaction, and the rows already written stay written.
    The response says how far it had got, because "cancelled" on its own leaves
    the user unable to decide whether to re-run the file.
    """
    account_id = _account_id(ctx)
    row = await imports.get_scoped(account_id, import_id)
    if row is None:
        return _fail("That import no longer exists.")
    if row.status in ("completed", "completed_with_errors", "failed", "cancelled"):
        # Not an error: the user clicked cancel as it finished, which is a race
        # they lost, not a mistake they made.
        return ResponseSchema(status="success",
                              message="That import had already finished.",
                              data=_details(row))

    updated = await imports.update_scoped(account_id, import_id, {
        "cancel_requested": True, "updated_at": _utcnow(),
    })
    return ResponseSchema(
        status="success",
        message=f"Stopping. {updated.processed_rows or 0} rows have already "
                f"been imported and will be kept.",
        data=_details(updated or row))


@router.get("/{import_id}/errors")
async def download_errors(
    import_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(READ),
    imports: CrmImportCrud = Depends(get_crud(CrmImportCrud)),
):
    """The rejected rows, as a CSV that lines up against the source file.

    Returns a file, not the envelope: the browser downloads this, and wrapping
    a CSV in JSON would mean the client had to unwrap and re-encode it.

    A 404 here rather than a 200-with-failure, because this endpoint's response
    is a file and there is no envelope to carry a business failure in.
    """
    row = await imports.get_scoped(_account_id(ctx), import_id)
    if row is None:
        return Response(status_code=404, content="Not found", media_type="text/plain")

    body = ir.error_csv(row.errors or [], row.headers or [])
    name = (row.file_name or "import").rsplit(".", 1)[0]
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}-errors.csv"',
            # The count is exact even when the rows in the file are capped, so
            # a user who sees 5,000 rows can tell whether that was all of them.
            "X-Error-Count": str(row.failed_count or 0),
            "X-Errors-Truncated": "true" if row.errors_truncated else "false",
        },
    )
