"""CRM contact profile CRUD. Phase 1 of docs/contact-unification-plan.md.

Reads and writes `crm_contact_profiles` — this account's relationship with a
person whose identity lives on the platform `contacts` document.

⚠️ **NOTHING CALLS THIS YET.** It ships with the model so phase 4 has its
primitives and phase 1 stays self-contained. Every method here is scoped by
`created_by`, like the rest of the `crm_*` CRUD, so the read path cannot
introduce a tenancy bug by forgetting one.

⚠️ **`list()` AND `count()` TAKE THE SAME BUILDER AND THE SAME ARGUMENTS**, so
a page and its total cannot disagree — the rule every other CRM list follows.
They drive from THIS collection, per plan decision 0.2, which is what keeps
owner, lifecycle and company index-served.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import logger as _logger
from app.models.contact_profile import CrmContactProfileModel
from app.services import queries as qb

from .base import CrmCrudBase


class CrmContactProfileCrud(CrmCrudBase):
    model = CrmContactProfileModel

    async def list(self, *, account_id: Any, sort_by: Optional[str] = None,
                   sort_dir: str = "asc", skip: int = 0, limit: int = 50,
                   **filters) -> List[CrmContactProfileModel]:
        """One page of this account's worked contacts.

        Ascending by name by default, like `CrmContactCrud.list`: the screen
        is read as a directory and newest-first makes looking somebody up a
        scroll.
        """
        return await self.list_by(
            qb.profile_filter(account_id=self.oid(account_id), **filters),
            sort=qb.profile_sort(sort_by, sort_dir), skip=skip, limit=limit,
        )

    async def count(self, *, account_id: Any, **filters) -> int:
        # Same builder, same arguments as list() — they cannot diverge.
        return await self.count_by(
            qb.profile_filter(account_id=self.oid(account_id), **filters))

    async def refresh_identity(self, *, account_id: Any, contact_id: Any,
                               full_name: Any = None, email: Any = None,
                               job_title: Any = None) -> bool:
        """Re-point the identity cache at what the person record now says.

        ⚠️ WRITES ONLY WHEN SOMETHING ACTUALLY CHANGED, which is what lets the
        reconcile job run over every profile nightly without touching
        `updated_at` on rows nobody edited — and `updated_at` is a sort key on
        this collection, so a blind `$set` would reshuffle a user's
        recently-touched view every night at 3am.
        """
        acct, cid = self.oid(account_id), self.oid(contact_id)
        if not acct or not cid or self.collection is None:
            return False
        wanted = {"full_name": full_name, "email": email, "job_title": job_title}
        try:
            res = await self.collection.update_one(
                {"created_by": acct, "contact_id": cid,
                 # The guard: match only when at least one field differs.
                 "$or": [{k: {"$ne": v}} for k, v in wanted.items()]},
                {"$set": wanted},
            )
            return bool(getattr(res, "modified_count", 0))
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return False

    async def for_contact(self, *, account_id: Any,
                          contact_id: Any) -> Optional[CrmContactProfileModel]:
        """This account's profile for one person, or None.

        None is a normal answer, not a miss: it means the person is in the
        marketing audience and nobody here has worked them yet.
        """
        acct, cid = self.oid(account_id), self.oid(contact_id)
        if not acct or not cid:
            return None
        return await self.model.find_one({"created_by": acct, "contact_id": cid})

    async def for_contacts(self, *, account_id: Any,
                           contact_ids: List[Any]) -> Dict[str, CrmContactProfileModel]:
        """Profiles for a page of people, indexed by `str(contact_id)`.

        ⚠️ ONE QUERY FOR A WHOLE PAGE, never one per row. The contact list
        renders 50 rows and every one of them needs its owner, lifecycle and
        rollups; a per-row read is the N+1 that `primary_company_name` is
        denormalised to avoid elsewhere in this module.

        Returns a dict rather than a list because the caller is zipping it back
        onto contacts it already has, in the order the contact query produced —
        not re-sorting profiles.
        """
        acct = self.oid(account_id)
        cids = self.oids(contact_ids)
        if not acct or not cids:
            return {}
        rows = await self.list_by(
            {"created_by": acct, "contact_id": {"$in": cids}},
            limit=len(cids))
        return {str(r.contact_id): r for r in rows}

    async def ensure(self, *, account_id: Any, contact_id: Any,
                     actor_user_id: Any = None,
                     defaults: Optional[Dict[str, Any]] = None
                     ) -> Optional[CrmContactProfileModel]:
        """The profile for this person, creating it if this account has none.

        ⚠️ **READ, INSERT, THEN READ AGAIN ON CONFLICT — not a pre-check.**
        Two requests editing the same unworked contact both find nothing and
        both insert; `uniq_crm_contact_profile` lets exactly one win. Catching
        the duplicate key and re-reading is what makes the loser correct
        instead of a 500. A pre-check alone cannot do this, because the gap
        between the check and the insert is the whole race.

        ⚠️ **`defaults` SEEDS A CREATE AND NEVER PATCHES AN EXISTING ROW.**
        An existing profile is returned untouched. If this merged defaults into
        a row that was already there, every read-ish call site would quietly
        rewrite `owner_id` or `lifecycle_stage` back to a default somebody had
        deliberately changed.
        """
        acct, cid = self.oid(account_id), self.oid(contact_id)
        if not acct or not cid:
            return None

        found = await self.for_contact(account_id=acct, contact_id=cid)
        if found is not None:
            return found

        doc = self.model(created_by=acct, contact_id=cid,
                         created_by_user_id=self.oid(actor_user_id),
                         **(defaults or {}))
        try:
            return await self.insert(doc)
        except Exception as ex:  # pylint: disable=broad-except
            # The index did its job: somebody else created it between the read
            # and the insert. Their row is as good as ours would have been.
            if ex.__class__.__name__ == "DuplicateKeyError":
                return await self.for_contact(account_id=acct, contact_id=cid)
            _logger.exception(ex)
            raise

    async def rename_company_fanout(self, *, account_id: Any, company_id: Any,
                                    new_name: str) -> int:
        """Refresh the denormalised ``primary_company_name``.

        Display-only, exactly like `CrmContactCrud.rename_company_fanout` and
        `CompanyCrud.rename_fanout`: a failure here degrades a label on a list
        and can never change behaviour. Phase 4 must call this alongside the
        existing ones, or a renamed company shows its old name on every
        unified contact row.
        """
        acct, cid = self.oid(account_id), self.oid(company_id)
        if not acct or not cid or self.db_session is None:
            return 0
        try:
            res = await self.db_session["crm_contact_profiles"].update_many(
                {"created_by": acct, "primary_company_id": cid},
                {"$set": {"primary_company_name": new_name}},
            )
            return int(getattr(res, "modified_count", 0) or 0)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return 0
