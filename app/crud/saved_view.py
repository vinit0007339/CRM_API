"""Saved-view CRUD. E23."""
from __future__ import annotations

from typing import Any, List, Optional

from app.models.saved_view import CrmSavedViewModel

from app.services import queries as qb

from .base import CrmCrudBase


class CrmSavedViewCrud(CrmCrudBase):
    model = CrmSavedViewModel

    async def list(self, *, account_id: Any, object_type: Optional[str] = None,
                   user_id: Any = None, limit: int = 200
                   ) -> List[CrmSavedViewModel]:
        """Every view in the account, optionally for one object type.

        ⚠️ ACCOUNT-WIDE ON PURPOSE, AND THEN NARROWED IN PYTHON. Visibility is
        "mine, plus shared" — two arms over two fields. Filtering it here would
        need an `$or` whose second arm (`is_shared`) no index serves anyway,
        and the rule then lives in a query instead of in
        `saved_view_rules.visible_to`, where it is tested. The row count per
        account is capped at `MAX_VIEWS` per object type, so this reads tens of
        documents, not thousands.
        """
        return await self.list_by(
            qb.saved_view_filter(account_id=self.oid(account_id),
                                 object_type=object_type,
                                 user_id=self.oid(user_id) if user_id else None),
            sort=[("name", 1)], limit=limit,
        )

    async def count(self, *, account_id: Any,
                    object_type: Optional[str] = None) -> int:
        return await self.count_by(
            qb.saved_view_filter(account_id=self.oid(account_id),
                                 object_type=object_type))

    async def clear_defaults(self, *, account_id: Any, user_id: Any,
                             object_type: str, keep_id: Any = None) -> int:
        """Unset `is_default` on this person's other views for this object.

        ⚠️ ONE DEFAULT PER PERSON PER OBJECT, AND IT IS ENFORCED HERE RATHER
        THAN BY AN INDEX. A unique partial index would also forbid a SHARED
        default coexisting with somebody's personal one, which is legitimate —
        the shared one is the house default and the personal one overrides it.
        """
        rows = await self.list(account_id=account_id, object_type=object_type)
        mine = [r for r in rows
                if str(r.created_by_user_id) == str(self.oid(user_id))
                and r.is_default
                and (keep_id is None or str(r.id) != str(keep_id))]
        for row in mine:
            row.is_default = False
            await row.save()
        return len(mine)
