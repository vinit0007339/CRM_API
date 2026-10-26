"""CrmViewPreferenceModel — one person's column layout. CRM-248.

⚠️ **KEYED ON (ACCOUNT, USER, OBJECT TYPE), AND THE ACCOUNT PART IS NOT
DECORATION.** A column layout could have lived on the user document, and it
would have been less machinery. It would also have been wrong: tenancy in this
module is ``created_by == account_id``, a user can belong to several accounts
(see ``UserModel.last_active_account_id``), and each account defines its own
custom attributes. One layout on the user document therefore travels between
accounts carrying column names the other account has never heard of — the list
view would silently prune them and the user would watch their columns
disappear when they switched accounts.

A collection rather than a field is also what makes saved views — currently
"not in Phase 1" — an added row rather than a migration.

One document per (created_by, user_id, object_type). ``columns`` is a list of
attribute ``internal_name``s in display order; what those names may be, and
what happens when one of them is deleted, is
``api/services/engage/crm/column_rules.py``.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.object_id import PyObjectId

from ._base import utcnow


class CrmViewPreferenceModel(Document):
    created_by: PyObjectId                      # account tenancy
    user_id: PyObjectId                         # whose layout this is

    object_type: str                            # company | deal

    # internal_names, in display order. NOT validated here — an attribute can
    # be deleted after this is written, so the stored list is a snapshot and
    # `column_rules.resolve` prunes it on read. Validation happens on write,
    # in the route, where refusing is still useful.
    columns: List[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "crm_view_preferences"
        indexes = [
            IndexModel(
                [("created_by", ASCENDING), ("user_id", ASCENDING),
                 ("object_type", ASCENDING)],
                unique=True,
                name="uniq_crm_view_preference",
            ),
        ]
