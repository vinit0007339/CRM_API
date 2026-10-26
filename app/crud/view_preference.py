"""View-preference CRUD. CRM-248."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.models.view_preference import CrmViewPreferenceModel

from app.services.queries import assert_account_scoped

from .base import CrmCrudBase


class CrmViewPreferenceCrud(CrmCrudBase):
    model = CrmViewPreferenceModel

    def build_filter(self, *, account_id: Any, user_id: Any,
                     object_type: Optional[str] = None) -> Dict[str, Any]:
        """ONE builder, so the read and the write cannot address different rows."""
        flt: Dict[str, Any] = {
            "created_by": self.oid(account_id),
            "user_id": self.oid(user_id),
        }
        if object_type:
            flt["object_type"] = object_type
        return assert_account_scoped(flt)

    async def get_for(self, account_id: Any, user_id: Any,
                      object_type: str) -> Optional[CrmViewPreferenceModel]:
        return await self.model.find_one(
            self.build_filter(account_id=account_id, user_id=user_id,
                              object_type=object_type))

    async def save_for(self, account_id: Any, user_id: Any, object_type: str,
                       columns) -> CrmViewPreferenceModel:
        """Upsert. Returns the stored document.

        Read-then-write rather than an atomic upsert because there is exactly
        one writer per row — the person whose layout it is, from one screen —
        so the race this would lose is a user racing themselves, and the loser
        is the layout they clicked second. The unique index is what makes a
        genuine double-submit fail loudly rather than create two rows.
        """
        existing = await self.get_for(account_id, user_id, object_type)
        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.columns = list(columns)
            existing.updated_at = now
            await existing.save()
            return existing

        doc = self.model(
            created_by=self.oid(account_id), user_id=self.oid(user_id),
            object_type=object_type, columns=list(columns),
            created_at=now, updated_at=now,
        )
        await doc.insert()
        return doc

    async def clear_for(self, account_id: Any, user_id: Any,
                        object_type: str) -> bool:
        """Reset to the shipped defaults by deleting the row.

        Deleting rather than storing the default list: "I never chose" and "I
        chose exactly today's default" then stay distinguishable, so a future
        change to the shipped layout reaches the first group and leaves the
        second alone.
        """
        existing = await self.get_for(account_id, user_id, object_type)
        if existing is None:
            return False
        await existing.delete()
        return True
