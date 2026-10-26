"""Task schemas. CRM-223.

Ids serialise as ``id``, never ``_id`` — the Phase 1 rule that cost a
production 422 on the board. Every schema here carries
``serialization_alias="id"`` and ``test_wire_format`` asserts it.

``due_bucket`` is computed server-side and sent with each row. The UI must not
re-derive it: the client's clock is not the account's timezone, and two
implementations of "today" is exactly how the label on a row stops matching the
group it was returned in.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.object_id import PyObjectId


class TaskReminderPayload(BaseModel):
    minutes_before: int = Field(0, ge=0, le=14 * 24 * 60)
    channels: List[Literal["email", "in_app"]] = Field(
        default_factory=lambda: ["in_app"])
    # sent_at is deliberately absent: it is the scheduler's claim, and a client
    # that could set it could suppress its own reminder — or replay one.


class ReminderState(TaskReminderPayload):
    """What is returned. Read-only where it differs from the payload."""

    sent_at: Optional[datetime] = None


class TaskLinks(BaseModel):
    company_ids: List[PyObjectId] = Field(default_factory=list)
    deal_ids: List[PyObjectId] = Field(default_factory=list)
    contact_ids: List[PyObjectId] = Field(default_factory=list)


class TaskCreateRequest(TaskLinks):
    title: str = Field(..., min_length=1, max_length=300)
    task_type: str = "todo"
    description: Optional[str] = Field(None, max_length=5000)
    due_at: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(None, ge=0, le=24 * 60)
    priority: Literal["low", "normal", "high"] = "normal"
    assigned_to_id: Optional[PyObjectId] = None
    reminder: Optional[TaskReminderPayload] = None

    @field_validator("title")
    @classmethod
    def not_only_whitespace(cls, value: str) -> str:
        # min_length=1 admits "   ", which saves a task with a blank row in the
        # list and no way to tell what it was.
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Give the task a title.")
        return cleaned

    @model_validator(mode="after")
    def reminder_needs_a_due_date(self):
        # A reminder is "N minutes before the due date". With no due date there
        # is nothing to be before, and the scheduler would never fire it —
        # silently, which is the worst way for a reminder to not happen.
        if self.reminder is not None and self.due_at is None:
            raise ValueError("A reminder needs a due date to count back from.")
        return self


class TaskUpdateRequest(BaseModel):
    """Every field optional. Absent means unchanged; explicit null means clear.

    The two are distinguished with ``model_fields_set``, because ``due_at:
    null`` is a real edit — removing a due date — and treating it as "unchanged"
    would make un-scheduling a task impossible.
    """

    title: Optional[str] = Field(None, min_length=1, max_length=300)
    task_type: Optional[str] = None
    description: Optional[str] = Field(None, max_length=5000)
    due_at: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(None, ge=0, le=24 * 60)
    priority: Optional[Literal["low", "normal", "high"]] = None
    assigned_to_id: Optional[PyObjectId] = None
    reminder: Optional[TaskReminderPayload] = None
    is_done: Optional[bool] = None
    company_ids: Optional[List[PyObjectId]] = None
    deal_ids: Optional[List[PyObjectId]] = None
    contact_ids: Optional[List[PyObjectId]] = None

    @field_validator("title")
    @classmethod
    def not_only_whitespace(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Give the task a title.")
        return value.strip() if value else value


class TaskDetails(BaseModel):
    id: PyObjectId = Field(alias="_id", serialization_alias="id")
    title: str
    task_type: str = "todo"
    description: Optional[str] = None

    due_at: Optional[datetime] = None
    due_bucket: Optional[str] = Field(
        None,
        description="Computed in the ACCOUNT's timezone. Do not re-derive "
                    "client-side; the browser's clock is not the account's.",
    )
    is_overdue: bool = False
    duration_minutes: Optional[int] = None
    priority: str = "normal"

    assigned_to_id: Optional[PyObjectId] = None
    assigned_to_name: Optional[str] = None

    is_done: bool = False
    completed_at: Optional[datetime] = None
    completed_by_id: Optional[PyObjectId] = None

    reminder: Optional[ReminderState] = None

    company_ids: List[PyObjectId] = Field(default_factory=list)
    deal_ids: List[PyObjectId] = Field(default_factory=list)
    contact_ids: List[PyObjectId] = Field(default_factory=list)

    # Filled only where the route resolved them — the list does not, the detail
    # does. Untyped, so everything inside goes through ``serializers.json_safe``
    # before it is put here: a raw ObjectId in a Dict[str, Any] has no
    # serializer and 500s at response time. That has already happened once, on
    # the activities endpoint.
    linked_records: List[Dict[str, Any]] = Field(default_factory=list)

    created_by_user_id: Optional[PyObjectId] = None
    created_by_name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def convert_id(cls, values):
        if isinstance(values, dict) and "_id" in values and "id" not in values:
            values["id"] = values.get("_id")
        return values

    model_config = {"populate_by_name": True}


class TaskListResponse(BaseModel):
    data: List[TaskDetails] = Field(default_factory=list)
    total: int = 0
    # Open-task counts per bucket, built from the same filter as the list.
    bucket_counts: Dict[str, int] = Field(default_factory=dict)
    status: str = "success"
    message: Optional[str] = ""


class BulkTaskRequest(BaseModel):
    task_ids: List[PyObjectId] = Field(..., min_length=1, max_length=200)
    action: Literal["complete", "assign", "reschedule", "delete"]
    assigned_to_id: Optional[PyObjectId] = None
    due_at: Optional[datetime] = None

    @model_validator(mode="after")
    def the_action_must_carry_its_argument(self):
        # `assign` with no assignee would unassign everything selected, and
        # `reschedule` with no date would clear every due date — both plausible
        # client bugs, both destructive, neither what anyone clicked.
        if self.action == "assign" and self.assigned_to_id is None:
            raise ValueError("Say who to assign these to.")
        if self.action == "reschedule" and self.due_at is None:
            raise ValueError("Say what date to move these to.")
        return self


class BulkTaskResult(BaseModel):
    """Per-id outcome, not just a count.

    A bulk action that reports "17 of 20 updated" without saying which three
    failed leaves the user to find them by hand.
    """

    updated: int = 0
    failed_ids: List[PyObjectId] = Field(default_factory=list)
    message: Optional[str] = None
