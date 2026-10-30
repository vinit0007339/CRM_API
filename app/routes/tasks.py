"""CRM tasks API. CRM-223 / CRM-224 / CRM-228.

Mounted at ``/api/v1/crm/tasks``::

    GET    ""              list, filtered; with per-bucket open counts
    POST   ""              create
    GET    "/{id}"         one task, with its linked records resolved
    PATCH  "/{id}"         edit
    DELETE "/{id}"         delete
    POST   "/{id}/complete"   · idempotent
    POST   "/bulk"         complete | assign | reschedule | delete

and ``/api/v1/crm/task-types`` (``types_router``) for the picklist.

TASKS HAVE THEIR OWN PERMISSION, unlike notes and files. That is not an
inconsistency: a note is *about* a deal and inherits from it, but a task is
assigned to a person, and "can work the task queue without being able to edit
the records" is a real role — it is exactly what the ``agent`` role in the RBAC
catalogue is. So ``crm_tasks:read`` / ``crm_tasks:write`` gate everything here,
and the linked deal or company is NOT re-checked.

⚠️ **Every write must go through ``_after_write``.** It is the one place the
counters (CRM-224) and the activity trail (CRM-228) are updated, and it takes
the task's state *before* and *after* so the deltas are computed from a real
pair rather than from an assumption about what changed. A write path that skips
it leaves the Kanban card's task badge wrong until the reconciliation script
next runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Path, Query

from app.crud import (
    CompanyCrud, CrmActivityCrud, CrmSettingsCrud, CrmTaskCrud, DealCrud,
)
from app.models.task import TASK_TYPES, CrmTaskModel, TaskReminder
from app.object_id import PyObjectId

from app.auth.context import RequestContext, require
from app.crud.dependency import get_crud
from app.schemas.common import ResponseSchema
from app.schemas.task import (
    BulkTaskRequest, BulkTaskResult, TaskCreateRequest, TaskDetails,
    TaskListResponse, TaskUpdateRequest,
)
from app.services import (
    activity_recorder as ar, serializers as sz, task_rules as tr,
)

router = APIRouter()
types_router = APIRouter()


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


async def _timezone_of(settings: CrmSettingsCrud, account_id: Any) -> Optional[str]:
    """The account's timezone, or None for the default.

    Read on every list request. It is one indexed document and the settings
    collection is tiny, but if this ever shows up in a profile it is a cache,
    not a reason to move the bucket maths to the client.
    """
    try:
        row = await settings.get_or_create(account_id=account_id)
        return getattr(row, "timezone", None) if row else None
    except Exception:  # pylint: disable=broad-except
        return None


def _details(task: CrmTaskModel, now: datetime,
             tz_name: Optional[str] = None,
             linked: Optional[List[Dict[str, Any]]] = None) -> TaskDetails:
    payload = task.model_dump(by_alias=True)
    payload["due_bucket"] = tr.bucket_of(task.due_at, now, tz_name,
                                         is_done=bool(task.is_done))
    payload["is_overdue"] = tr.is_overdue(task.due_at, now,
                                          is_done=bool(task.is_done))
    # json_safe, not the raw dicts: linked_records is Dict[str, Any], and an
    # ObjectId inside an untyped container has no serializer. That exact bug
    # 500'd the activities endpoint in Phase 1.
    payload["linked_records"] = sz.json_safe(linked or [])
    return TaskDetails.model_validate(payload)


def _state(task: Optional[CrmTaskModel]) -> Optional[Dict[str, Any]]:
    """The two fields the counters care about, snapshotted."""
    if task is None:
        return None
    return {"due_at": task.due_at, "is_done": bool(task.is_done)}


async def _after_write(
    crud: CrmTaskCrud, account_id: Any, *,
    before: Optional[CrmTaskModel], after: Optional[CrmTaskModel], now: datetime,
) -> None:
    """Update the counters for a single task write. CRM-224.

    Takes both states because a task's LINKS can change too: moving a task from
    deal A to deal B has to decrement A and increment B, which a single "here
    is the new task" call could not know.
    """
    before_links = tr.linked_ids(before.model_dump()) if before else []
    after_links = tr.linked_ids(after.model_dump()) if after else []

    removed = [link for link in before_links if link not in after_links]
    added = [link for link in after_links if link not in before_links]
    common = [link for link in after_links if link in before_links]

    # Links it left: the whole of its old contribution goes away.
    if removed:
        await crud.apply_counter_deltas(
            account_id=account_id, links=removed,
            deltas=tr.counter_deltas(_state(before), None, now))
    # Links it joined: the whole of its new contribution arrives.
    if added:
        await crud.apply_counter_deltas(
            account_id=account_id, links=added,
            deltas=tr.counter_deltas(None, _state(after), now))
    # Links it kept: only the difference.
    if common:
        await crud.apply_counter_deltas(
            account_id=account_id, links=common,
            deltas=tr.counter_deltas(_state(before), _state(after), now))

    due_changed = (before.due_at if before else None) != (after.due_at if after else None)
    done_changed = bool(before and before.is_done) != bool(after and after.is_done)
    if due_changed or done_changed or removed or added:
        await crud.refresh_next_activity(
            account_id=account_id, links=list({*before_links, *after_links}))


async def _record(activity: CrmActivityCrud, ctx: RequestContext,
                  task: CrmTaskModel, kind: str, title: str) -> None:
    """One activity entry per linked record. CRM-228.

    A task on a deal AND its company writes to both timelines: someone reading
    the company's history should see that a call was scheduled on one of its
    deals, and someone reading the deal should not have to look up.
    """
    account_id = _account_id(ctx)
    for entity_type, entity_id in tr.linked_ids(task.model_dump()):
        try:
            await ar.record(activity.write, ar.build_activity(
                account_id=account_id, entity_type=entity_type,
                entity_id=entity_id, activity_type=kind, title=title,
                # json_safe: metadata is Dict[str, Any] and an ObjectId in an
                # untyped container has no serializer. That bug 500'd the
                # activities endpoint in Phase 1.
                # ⚠️ due_at IS A SNAPSHOT, NOT A POINTER. A timeline row says
                # what was true when it happened; re-reading the task at
                # render time would rewrite history every time the date moved.
                #
                # ⚠️ AND NO DONE FLAG HERE. A "task created" row is written
                # when nothing is done yet, so storing `is_done` would freeze
                # a False that later turns true and let the reader paint a
                # finished task as overdue forever. Whether it was finished is
                # a LATER row on the same `task_id`, which the reader already
                # has — so it computes "overdue" from the feed, not from here.
                metadata=sz.json_safe({"task_id": task.id,
                                       "task_title": task.title,
                                       "task_type": task.task_type,
                                       "task_due_at": task.due_at}),
                actor_user_id=ctx.user.id, actor_name=_actor_name(ctx)))
        except Exception:  # pylint: disable=broad-except
            # The activity trail must never fail the write it describes.
            pass


# ── read ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: Optional[str] = Query(None, pattern="^(todo|done|all)$"),
    assigned_to_id: Optional[PyObjectId] = Query(None),
    assigned_to_me: bool = Query(
        False,
        description="Resolved from the token. Prefer this to sending your own "
                    "user id — a client that reads the current user from its "
                    "own store and gets it wrong shows someone another "
                    "person's task list, and nothing about that looks broken.",
    ),
    task_type: Optional[str] = Query(None),
    priority: Optional[str] = Query(None, pattern="^(low|normal|high)$"),
    due: Optional[str] = Query(
        None, pattern="^(overdue|today|tomorrow|week|later|range|no_due_date)$"),
    due_from: Optional[datetime] = Query(None),
    due_to: Optional[datetime] = Query(None),
    deal_id: Optional[PyObjectId] = Query(None),
    company_id: Optional[PyObjectId] = Query(None),
    contact_id: Optional[PyObjectId] = Query(None),
    search: Optional[str] = Query(None, max_length=120),
    sort_by: Optional[str] = Query(None),
    sort_dir: Optional[str] = Query(None, pattern="^(asc|desc)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    with_counts: bool = Query(False),
    ctx: RequestContext = Depends(require("crm_tasks:read")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Tasks, filtered. ``due`` resolves in the ACCOUNT's timezone.

    ``now`` is captured ONCE and threaded through the filter, the bucket counts
    and every row's label. Calling ``datetime.now()`` in three places would let
    a task cross the overdue boundary between the query and the labelling, and
    the row would come back tagged with a bucket the filter did not select.
    """
    account_id = _account_id(ctx)
    now = _utcnow()
    tz_name = await _timezone_of(settings, account_id)

    # An explicit id wins, so "everyone's tasks, filtered to Arjun" still works.
    if assigned_to_me and assigned_to_id is None:
        assigned_to_id = ctx.user.id

    filters = crud.build_filter(
        account_id=account_id, status=status, assigned_to_id=assigned_to_id,
        task_type=task_type, priority=priority, due=due, due_from=due_from,
        due_to=due_to, deal_id=deal_id, company_id=company_id,
        contact_id=contact_id, search=search, now=now, tz_name=tz_name)

    rows = await crud.list_tasks(filters, sort=tr.sort_spec(sort_by, sort_dir),
                                 skip=skip, limit=limit)
    total = await crud.count_tasks(filters)

    counts: Dict[str, int] = {}
    if with_counts:
        counts = await crud.bucket_counts(
            account_id=account_id, assigned_to_id=assigned_to_id, now=now,
            tz_name=tz_name)

    return TaskListResponse(
        data=[_details(row, now, tz_name) for row in rows],
        total=total, bucket_counts=counts, status="success")


@router.get("/{task_id}", response_model=ResponseSchema[TaskDetails])
async def get_task(
    task_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("crm_tasks:read")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
):
    account_id = _account_id(ctx)
    task = await crud.get_scoped(account_id, task_id)
    if task is None:
        return _fail("That task no longer exists.")

    now = _utcnow()
    tz_name = await _timezone_of(settings, account_id)
    linked = await _resolve_links(task, account_id, companies, deals)
    return ResponseSchema(status="success", message="",
                          data=_details(task, now, tz_name, linked))


async def _resolve_links(task: CrmTaskModel, account_id: Any,
                         companies: CompanyCrud, deals: DealCrud
                         ) -> List[Dict[str, Any]]:
    """Names for the records a task is attached to, so the detail panel can
    show "Acme Ltd" rather than an ObjectId.

    Scoped lookups, not ``Model.get`` — a task carrying another account's id
    (however it got there) must resolve to nothing rather than to their data.
    """
    out: List[Dict[str, Any]] = []
    for record_id in task.deal_ids or []:
        row = await deals.get_scoped(account_id, record_id)
        if row:
            out.append({"entity_type": "deal", "id": row.id, "name": row.name})
    for record_id in task.company_ids or []:
        row = await companies.get_scoped(account_id, record_id)
        if row:
            out.append({"entity_type": "company", "id": row.id, "name": row.name})
    for record_id in task.contact_ids or []:
        # Contacts are not ours; the id is returned without a name rather than
        # reaching into another module's collection from here.
        out.append({"entity_type": "contact", "id": record_id, "name": None})
    return out


@types_router.get("", response_model=ResponseSchema[List[str]])
async def list_task_types(
    ctx: RequestContext = Depends(require("crm_tasks:read")),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """The built-in types plus the account's custom ones, de-duplicated.

    Built-ins first and in a fixed order: this list is a dropdown, and a
    dropdown whose options reorder between page loads is unusable.
    """
    row = await settings.get_or_create(account_id=_account_id(ctx))
    custom = list(getattr(row, "task_types", None) or []) if row else []
    ordered = list(TASK_TYPES) + [t for t in custom if t not in TASK_TYPES]
    return ResponseSchema(status="success", message="", data=ordered)


# ── write ────────────────────────────────────────────────────────────────────
@router.post("", response_model=ResponseSchema[TaskDetails])
async def create_task(
    payload: TaskCreateRequest,
    ctx: RequestContext = Depends(require("crm_tasks:write")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)
    now = _utcnow()

    task = CrmTaskModel(
        created_by=account_id,
        title=payload.title,
        task_type=payload.task_type or "todo",
        description=payload.description,
        duration_minutes=payload.duration_minutes,
        priority=payload.priority,
        # Unassigned tasks are nobody's problem and are never looked at again,
        # so an omitted assignee means the person creating it.
        assigned_to_id=payload.assigned_to_id or ctx.user.id,
        assigned_to_name=(None if payload.assigned_to_id else _actor_name(ctx)),
        reminder=(TaskReminder(**payload.reminder.model_dump())
                  if payload.reminder else None),
        company_ids=payload.company_ids or [],
        deal_ids=payload.deal_ids or [],
        contact_ids=payload.contact_ids or [],
        created_by_user_id=ctx.user.id,
        created_by_name=_actor_name(ctx),
        created_at=now, updated_at=now,
    )
    for field, value in tr.due_fields(payload.due_at).items():
        setattr(task, field, value)

    created = await crud.insert(task)
    if not created:
        return _fail("Could not save that task.")

    await _after_write(crud, account_id, before=None, after=created, now=now)
    await _record(activity, ctx, created, "task_created",
                  f"Task created: {created.title}")

    tz_name = await _timezone_of(settings, account_id)
    return ResponseSchema(status="success", message="Task created.",
                          data=_details(created, now, tz_name))


@router.patch("/{task_id}", response_model=ResponseSchema[TaskDetails])
async def update_task(
    payload: TaskUpdateRequest,
    task_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("crm_tasks:write")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)
    existing = await crud.get_scoped(account_id, task_id)
    if existing is None:
        return _fail("That task no longer exists.")

    # Snapshot BEFORE mutating: `existing` is the same object the update writes
    # through, so a reference kept here would show the new values.
    before = existing.model_copy(deep=True)
    now = _utcnow()
    tz_name = await _timezone_of(settings, account_id)
    sent = payload.model_fields_set          # explicit null ≠ absent

    changes: Dict[str, Any] = {}
    for field in ("title", "task_type", "description", "duration_minutes",
                  "priority", "assigned_to_id", "company_ids", "deal_ids",
                  "contact_ids"):
        if field in sent:
            changes[field] = getattr(payload, field)

    if "assigned_to_id" in sent:
        # The stored name goes stale the moment the assignee changes; clearing
        # it makes the UI resolve the id rather than show the previous person.
        changes["assigned_to_name"] = None

    if "due_at" in sent:
        changes.update(tr.due_fields(payload.due_at))
    if "reminder" in sent:
        changes["reminder"] = (TaskReminder(**payload.reminder.model_dump())
                               if payload.reminder else None)
    if "is_done" in sent:
        changes.update(_completion_changes(bool(payload.is_done), ctx, now))

    if not changes:
        return ResponseSchema(status="success", message="Nothing to change.",
                              data=_details(existing, now, tz_name))

    if changes.get("reminder") is not None:
        due = changes.get("due_at", existing.due_at)
        if due is None:
            return _fail("A reminder needs a due date to count back from.")

    changes["updated_at"] = now
    updated = await crud.update_scoped(account_id, task_id, changes)
    if updated is None:
        return _fail("Could not update that task.")

    await _after_write(crud, account_id, before=before, after=updated, now=now)
    await _record_update(activity, ctx, before, updated, sent)

    return ResponseSchema(status="success", message="Task updated.",
                          data=_details(updated, now, tz_name))


def _completion_changes(done: bool, ctx: RequestContext,
                        now: datetime) -> Dict[str, Any]:
    """Completing and reopening are symmetric — reopening CLEARS the stamps.

    Leaving ``completed_at`` set on a reopened task would put it in the "done"
    filter's date range while showing as open, which is the kind of
    contradiction nobody reports and everybody notices.
    """
    if done:
        return {"is_done": True, "completed_at": now,
                "completed_by_id": ctx.user.id}
    return {"is_done": False, "completed_at": None, "completed_by_id": None}


async def _record_update(activity: CrmActivityCrud, ctx: RequestContext,
                         before: CrmTaskModel, after: CrmTaskModel,
                         sent: set) -> None:
    """CRM-228: completed / reassigned / rescheduled each get their own entry.

    Not one generic "task updated" — the timeline is read to answer "when did
    this slip?", and an entry that does not say what changed cannot answer it.
    """
    if "is_done" in sent and before.is_done != after.is_done:
        await _record(activity, ctx, after,
                      "task_completed" if after.is_done else "task_reopened",
                      (f"Task completed: {after.title}" if after.is_done
                       else f"Task reopened: {after.title}"))
    if "assigned_to_id" in sent and before.assigned_to_id != after.assigned_to_id:
        await _record(activity, ctx, after, "task_reassigned",
                      f"Task reassigned: {after.title}")
    if "due_at" in sent and before.due_at != after.due_at:
        await _record(activity, ctx, after, "task_rescheduled",
                      f"Task rescheduled: {after.title}")


@router.post("/{task_id}/complete", response_model=ResponseSchema[TaskDetails])
async def complete_task(
    task_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("crm_tasks:write")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """Mark a task done. IDEMPOTENT — completing a done task is a success.

    The checkbox is the most double-clicked control in any task list, and the
    request is often retried on a flaky connection. An error the second time
    would tell the user their click failed when it did not.
    """
    account_id = _account_id(ctx)
    existing = await crud.get_scoped(account_id, task_id)
    if existing is None:
        return _fail("That task no longer exists.")

    now = _utcnow()
    tz_name = await _timezone_of(settings, account_id)
    if existing.is_done:
        return ResponseSchema(status="success", message="Already completed.",
                              data=_details(existing, now, tz_name))

    before = existing.model_copy(deep=True)
    updated = await crud.update_scoped(
        account_id, task_id, {**_completion_changes(True, ctx, now),
                              "updated_at": now})
    if updated is None:
        return _fail("Could not complete that task.")

    await _after_write(crud, account_id, before=before, after=updated, now=now)
    await _record(activity, ctx, updated, "task_completed",
                  f"Task completed: {updated.title}")
    return ResponseSchema(status="success", message="Task completed.",
                          data=_details(updated, now, tz_name))


@router.delete("/{task_id}", response_model=ResponseSchema[Dict[str, Any]])
async def delete_task(
    task_id: PyObjectId = Path(...),
    ctx: RequestContext = Depends(require("crm_tasks:write")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    account_id = _account_id(ctx)
    existing = await crud.get_scoped(account_id, task_id)
    if existing is None:
        return _fail("That task no longer exists.")

    before = existing.model_copy(deep=True)
    now = _utcnow()
    if not await crud.delete_scoped(account_id, task_id):
        return _fail("Could not delete that task.")

    await _after_write(crud, account_id, before=before, after=None, now=now)
    await _record(activity, ctx, before, "task_deleted",
                  f"Task deleted: {before.title}")
    return ResponseSchema(status="success", message="Task deleted.",
                          data={"id": str(task_id)})


@router.post("/bulk", response_model=ResponseSchema[BulkTaskResult])
async def bulk_tasks(
    payload: BulkTaskRequest,
    ctx: RequestContext = Depends(require("crm_tasks:write")),
    crud: CrmTaskCrud = Depends(get_crud(CrmTaskCrud)),
    activity: CrmActivityCrud = Depends(get_crud(CrmActivityCrud)),
):
    """complete | assign | reschedule | delete, one task at a time.

    NOT an ``update_many``. Each task needs its own before/after pair for the
    counters and its own activity entry, and a single bulk statement can give
    neither. Fifty round trips for a fifty-task selection is the price of the
    counters being right.

    PARTIAL SUCCESS IS REPORTED, not swallowed. A task that has been deleted by
    someone else mid-selection is listed in ``failed_ids`` rather than silently
    counted as done.
    """
    account_id = _account_id(ctx)
    now = _utcnow()
    updated, failed = 0, []

    for task_id in payload.task_ids:
        existing = await crud.get_scoped(account_id, task_id)
        if existing is None:
            failed.append(task_id)
            continue
        before = existing.model_copy(deep=True)

        try:
            if payload.action == "delete":
                if not await crud.delete_scoped(account_id, task_id):
                    failed.append(task_id)
                    continue
                await _after_write(crud, account_id, before=before, after=None,
                                   now=now)
                await _record(activity, ctx, before, "task_deleted",
                              f"Task deleted: {before.title}")
                updated += 1
                continue

            if payload.action == "complete":
                if existing.is_done:
                    updated += 1          # idempotent, as for the single call
                    continue
                changes = _completion_changes(True, ctx, now)
                kind, title = "task_completed", f"Task completed: {before.title}"
            elif payload.action == "assign":
                changes = {"assigned_to_id": payload.assigned_to_id,
                           "assigned_to_name": None}
                kind, title = "task_reassigned", f"Task reassigned: {before.title}"
            else:                          # reschedule
                changes = dict(tr.due_fields(payload.due_at))
                kind, title = "task_rescheduled", f"Task rescheduled: {before.title}"

            changes["updated_at"] = now
            after = await crud.update_scoped(account_id, task_id, changes)
            if after is None:
                failed.append(task_id)
                continue
            await _after_write(crud, account_id, before=before, after=after,
                               now=now)
            await _record(activity, ctx, after, kind, title)
            updated += 1
        except Exception:  # pylint: disable=broad-except
            # One bad row must not abandon the rest of the selection.
            failed.append(task_id)

    result = BulkTaskResult(
        updated=updated, failed_ids=failed,
        message=(None if not failed
                 else f"{len(failed)} of {len(payload.task_ids)} could not be updated."))
    return ResponseSchema(status="success", message="", data=result)
