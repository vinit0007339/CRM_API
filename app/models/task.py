"""CrmTaskModel — collection ``crm_tasks``. CRM-222.

Per requirements §6.6. A task links to records via **direct id arrays** rather
than through ``crm_associations``, which is a deliberate inconsistency with
every other pair in this module: a task is a leaf object, never traversed in
reverse at scale, and an array-membership index answers "tasks on this deal" in
one hop instead of two.

``due_at`` is stored as UTC and bucketed in the account's timezone — see
``api/services/engage/crm/task_rules.py``. Nothing in this model knows about
"today"; that word only means something once a timezone is applied.

``reminder.sent_at`` is not just a record of what happened — it is the CLAIM
that stops two scheduler processes sending the same reminder. See
``task_rules.claim_filter``.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow

TASK_TYPES = ("todo", "call", "email", "meeting", "deadline", "linkedin", "lunch")
PRIORITIES = ("low", "normal", "high")


class TaskReminder(BaseModel):
    minutes_before: int = 0
    channels: List[Literal["email", "in_app"]] = Field(default_factory=lambda: ["in_app"])

    # Set by the scheduler's conditional update. ``None`` means unclaimed; any
    # value means some process has taken it. Do not write this from a handler.
    sent_at: Optional[datetime] = None


class CrmTaskModel(Document):
    created_by: PyObjectId                      # account tenancy

    title: str
    task_type: str = "todo"                     # + custom types from settings
    description: Optional[str] = None

    due_at: Optional[datetime] = None           # UTC; may be absent
    # Redundant with ``due_at is not None`` — deliberately. Mongo sorts a null
    # date BEFORE every real one, so "soonest first" would open the list with
    # every unscheduled task. Sorting on this first fixes that without an
    # aggregation. Written ONLY via ``task_rules.due_fields``; never set it
    # alone.
    has_due_date: bool = False
    duration_minutes: Optional[int] = None
    priority: str = "normal"

    assigned_to_id: Optional[PyObjectId] = None
    assigned_to_name: Optional[str] = None

    is_done: bool = False
    completed_at: Optional[datetime] = None
    completed_by_id: Optional[PyObjectId] = None

    reminder: Optional[TaskReminder] = None

    # Direct links — see the module docstring for why these are not associations.
    company_ids: List[PyObjectId] = Field(default_factory=list)
    deal_ids: List[PyObjectId] = Field(default_factory=list)
    contact_ids: List[PyObjectId] = Field(default_factory=list)

    created_by_user_id: Optional[PyObjectId] = None
    created_by_name: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_tasks"
        indexes = [
            # "the account's open tasks by due date" — the Tasks screen with no
            # assignee filter, and every due-bucket count.
            # has_due_date sits between the equality keys and the range key on
            # purpose: every due-window filter adds it as an equality
            # predicate, which is the one shape Mongo can use for the window
            # AND the sort in a single index pass.
            IndexModel([("created_by", ASCENDING), ("is_done", ASCENDING),
                        ("has_due_date", ASCENDING), ("due_at", ASCENDING)],
                       name="tasks_by_due"),
            # "my open tasks by due date" — the screen's default view, which
            # filters by assignee before anything else.
            IndexModel([("created_by", ASCENDING), ("assigned_to_id", ASCENDING),
                        ("is_done", ASCENDING), ("has_due_date", ASCENDING),
                        ("due_at", ASCENDING)], name="tasks_by_assignee_due"),
            # Array-membership indexes: "tasks on this record", one hop each.
            IndexModel([("created_by", ASCENDING), ("deal_ids", ASCENDING)],
                       name="tasks_by_deal"),
            IndexModel([("created_by", ASCENDING), ("company_ids", ASCENDING)],
                       name="tasks_by_company"),
            IndexModel([("created_by", ASCENDING), ("contact_ids", ASCENDING)],
                       name="tasks_by_contact"),
            # The scheduler's sweep: unsent reminders whose task is still open.
            # Without this it is a full collection scan every minute, forever.
            IndexModel([("reminder.sent_at", ASCENDING), ("is_done", ASCENDING),
                        ("due_at", ASCENDING)], name="tasks_reminder_sweep",
                       sparse=True),
        ]
