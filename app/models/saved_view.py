"""CrmSavedViewModel — a named filter for a list screen. E23.

⚠️ **THE FILTER IS STORED AS THE GROUP, NOT AS A MONGO FRAGMENT.** It would be
tempting to keep the compiled fragment: the list could then merge it without
re-compiling. It would also mean a document in the database holds query
operators, which is a query a client once chose, kept forever, replayed on
every click, and never re-validated against the rules that have changed since.
Storing the GROUP means `filter_rules` compiles it fresh every time, so a rule
tightened today applies to a view saved last year.

⚠️ **IT IS A SNAPSHOT.** `filter_group` names fields; `columns` names
attributes. Either can be deleted after the view is saved. Nothing here
validates that — `saved_view_rules` validates on write and REPORTS staleness on
read, because a view that has gone stale must stay listable and deletable.

Scoped `(created_by, object_type)` for the list, with `created_by_user_id`
deciding ownership and `is_shared` deciding visibility. No unique index on the
name: uniqueness depends on who will SEE the view (see
`saved_view_rules.name_is_free`), which is not a shape an index can express —
two people may each have a personal "My deals".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class CrmSavedViewModel(Document):
    created_by: PyObjectId                      # account tenancy
    created_by_user_id: PyObjectId              # whose view this is

    object_type: str                            # company | deal
    name: str

    # The group the user built, exactly as `filter_rules.parse` takes it.
    filter_group: Dict[str, Any] = Field(default_factory=dict)

    # The sort and the columns that made this view readable. Optional: a view
    # that only says "these rows" leaves the screen's own choices alone, which
    # is what most views want.
    columns: List[str] = Field(default_factory=list)
    sort_by: Optional[str] = None
    sort_dir: str = "desc"

    # On everybody's toolbar, or only its owner's.
    is_shared: bool = False
    # The view this person's list opens on. One per person per object type —
    # enforced in the route by clearing the others, not by an index, because a
    # shared default and a personal default legitimately coexist.
    is_default: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_saved_views"
        indexes = [
            IndexModel(
                [("created_by", ASCENDING), ("object_type", ASCENDING),
                 ("name", ASCENDING)],
                name="crm_saved_view_account_object_name",
            ),
            IndexModel(
                [("created_by", ASCENDING), ("created_by_user_id", ASCENDING),
                 ("object_type", ASCENDING)],
                name="crm_saved_view_owner",
            ),
        ]
