"""CRM settings + attribute CRUD. CRM-019."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import logger as _logger
from app.models.attribute import CrmAttributeModel
from app.models.settings import CrmSettingsModel

from app.services import queries as qb

from .base import CrmCrudBase


class CrmSettingsCrud(CrmCrudBase):
    model = CrmSettingsModel

    async def get_or_create(self, *, account_id: Any) -> Optional[CrmSettingsModel]:
        """One document per account.

        Read-mostly: only writes when the account genuinely has no settings yet,
        so a plain GET does not turn into a write on every request.
        """
        acct = self.oid(account_id)
        if not acct:
            return None
        try:
            existing = await CrmSettingsModel.find_one({"created_by": acct})
            if existing:
                return existing
            # WITH the defaults, not a bare model. A bare insert left
            # `lost_reasons`, `industries` and `task_types` all empty, and
            # nothing else ever filled them: `seed_crm_defaults.py` is the only
            # writer and nothing runs it per account. Pipelines have
            # auto-provisioned on first read since Phase 1; settings had not.
            #
            # ⚠️ THIS BLOCKED CLOSE-AS-LOST ENTIRELY. There is no UI to add a
            # lost reason, the dialog's picker is `required`, and
            # `deals.close_deal` validates the chosen id against this list — so
            # with an empty list a deal could not be marked lost at all.
            from app.services.defaults import default_settings_payload

            return await CrmSettingsModel(
                created_by=acct, **default_settings_payload()).insert()
        except Exception as ex:  # pylint: disable=broad-except
            # A racing create hits the unique index; re-read rather than fail.
            _logger.warning(f"crm_settings get_or_create: {ex}")
            try:
                return await CrmSettingsModel.find_one({"created_by": acct})
            except Exception:  # pylint: disable=broad-except
                return None


class CrmAttributeCrud(CrmCrudBase):
    model = CrmAttributeModel

    async def list(self, *, account_id: Any, object_type: Optional[str] = None,
                   show_on_form: Optional[bool] = None) -> List[CrmAttributeModel]:
        return await self.list_by(
            qb.attribute_filter(account_id=self.oid(account_id),
                                object_type=object_type, show_on_form=show_on_form),
            sort=[("form_order", 1)], limit=200,
        )

    async def by_internal_name(self, *, account_id: Any, object_type: str,
                               internal_name: str) -> Optional[CrmAttributeModel]:
        acct = self.oid(account_id)
        if not acct:
            return None
        try:
            return await CrmAttributeModel.find_one({
                "created_by": acct, "object_type": object_type,
                "internal_name": internal_name,
            })
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return None

    async def count_system(self, *, account_id: Any) -> int:
        """How many ``is_system`` rows this account holds. CRM-P0-002.

        The cheap guard on the read path: compared against
        ``defaults.system_attribute_count()`` to decide whether provisioning
        needs to look any further. Returns ``-1`` on failure, because unknown is
        not zero — a failed count must not be read as "provision everything".
        """
        acct = self.oid(account_id)
        if not acct:
            return -1
        try:
            return await self.count_by({"created_by": acct, "is_system": True})
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return -1

    async def system_pairs(self, *, account_id: Any) -> List[tuple]:
        """``(object_type, internal_name)`` for every ``is_system`` row."""
        acct = self.oid(account_id)
        if not acct:
            return []
        try:
            rows = await self.list_by(
                {"created_by": acct, "is_system": True}, limit=500,
            )
            return [(r.object_type, r.internal_name) for r in rows]
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return []

    async def insert_system(
        self, *, account_id: Any, rows: List[Dict[str, Any]]
    ) -> int:
        """Insert missing system attribute rows. Returns how many landed.

        Inserted one at a time on purpose: a duplicate losing a race with a
        concurrent provisioner must not abort the rest of the batch.
        """
        acct = self.oid(account_id)
        if not acct or not rows:
            return 0
        written = 0
        for row in rows:
            try:
                await CrmAttributeModel(created_by=acct, **row).insert()
                written += 1
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)
        return written

    async def retype_system(
        self, *, account_id: Any, rows: List[Dict[str, Any]]
    ) -> int:
        """Correct the ``attribute_type`` of system rows seeded with the wrong
        one. Returns how many rows changed. Feeds on
        ``defaults.system_attribute_retypes()``.

        ⚠️ `attribute_type` IS IN THE FILTER, NOT JUST THE UPDATE. Matching
        only on the name would rewrite the type on every read, overwriting
        whatever the row holds; matching on the wrong type makes this a no-op
        from the second call onward and leaves any other value alone.

        ⚠️ `is_system` IS IN THE FILTER TOO. A custom attribute an account
        happened to name `industry` is its own field and is not ours to retype.

        One `update_many` per entry, each guarded: a failure on one must not
        abandon the rest, and none of them may fail the read that called this.
        """
        acct = self.oid(account_id)
        if not acct or not rows:
            return 0
        changed = 0
        for row in rows:
            try:
                res = await CrmAttributeModel.get_motor_collection().update_many(
                    {
                        "created_by": acct,
                        "object_type": row["object_type"],
                        "internal_name": row["internal_name"],
                        "attribute_type": row["from_type"],
                        "is_system": True,
                    },
                    {"$set": {"attribute_type": row["to_type"]}},
                )
                changed += int(res.modified_count or 0)
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)
        return changed

    async def labels_for(self, *, account_id: Any, object_type: str) -> dict:
        """``{internal_name: label}`` — feeds the required-attribute messages
        that ``stage_rules.validate_transition`` puts in front of the user.
        """
        rows = await self.list(account_id=account_id, object_type=object_type)
        return {r.internal_name: r.label for r in rows}
