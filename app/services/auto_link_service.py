"""Find-or-create the company for a contact's email domain, and link them.
CRM-242.

Called from the authenticated contact paths only — the contact API and the CSV
list import. The public paths that also create contacts (landing pages, signup
forms, quizzes, event ingestion, webhooks) deliberately do NOT trigger this: an
unauthenticated visitor filling in a form should not be able to cause a company
record to appear in someone's CRM.

⚠️ **This runs inside somebody else's request.** The contact API is not part of
the CRM, and a CRM feature that makes contact creation fail — or slow — is a
worse outcome than a contact that is not linked. So every failure here is
swallowed and logged, and the caller is told nothing went wrong, because from
the caller's point of view nothing did.

**Linking only ever adds.** On an email change the contact gains a link to the
new domain's company; the old one stays. Removing it would be the system
deleting a relationship a person may have curated — someone who moved from one
customer to another is genuinely connected to both — and an extra link is
visible and deletable, while a missing one is not.


⚠️ NO CALLER AS OF PHASE 4a, AND THAT IS A STATED GAP, NOT AN OVERSIGHT. The
only thing that drove this was `POST /api/v1/internal/crm/auto-link`, which
phase 4a deleted along with the platform hook that called it: creating a
MARKETING contact no longer reaches into the CRM. Domain-based company linking
is still wanted — phase 5 drives it from `POST /crm/contacts`, where the
contact being linked is the CRM's own.

Kept rather than deleted-and-restored because phase 5 is next and the churn
would be pure. But `aee58ca`'s lesson applies and is worth repeating here:
"Dead code that still imports cleanly gets called again." If phase 5 changes
shape and this ends up with no caller after it, delete it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import logger as _logger
from app.models.company import CompanyModel
from app.object_id import PyObjectId

from app.services import auto_link as al, dedupe


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AutoLinker:
    """One account's auto-linking, reusable across a batch.

    ``_cache`` matters for the CSV import: five thousand contacts at the same
    company would otherwise be five thousand identical lookups, and — worse —
    a race with itself that creates the company more than once.
    """

    def __init__(self, *, account_id: Any, companies=None, associations=None,
                 settings=None, actor_user_id: Any = None):
        self.account_id = PyObjectId(str(account_id))
        self.companies = companies
        self.associations = associations
        self.settings_crud = settings
        self.actor_user_id = actor_user_id
        self._settings = None
        self._cache: Dict[str, Any] = {}
        self.stats = {"linked": 0, "created": 0, "skipped": 0}

    async def settings(self):
        if self._settings is None:
            self._settings = await self.settings_crud.get_or_create(
                account_id=self.account_id)
        return self._settings

    async def link_contact(self, contact_id: Any, email: Optional[str],
                           existing_domains: Optional[List[str]] = None
                           ) -> Dict[str, Any]:
        """Link one contact. Never raises.

        Returns the decision so a caller that wants to report on a batch can,
        and so the reason a contact was skipped is available rather than
        inferred.
        """
        try:
            settings = await self.settings()
            decision = al.link_decision(email, settings, existing_domains)
            if not decision["should_link"]:
                self.stats["skipped"] += 1
                return decision

            company = await self._company_for(decision["domain"],
                                              decision["company_name"])
            if company is None:
                self.stats["skipped"] += 1
                return {**decision, "should_link": False, "reason": "no_company"}

            await self.associations.link(
                account_id=self.account_id, from_type="company",
                from_id=company.id, to_type="contact", to_id=contact_id,
                actor_user_id=self.actor_user_id)
            self.stats["linked"] += 1
            return {**decision, "company_id": company.id}
        except Exception as ex:  # pylint: disable=broad-except
            # See the module docstring: this runs inside the contact API's
            # request, and a CRM convenience must never fail somebody else's
            # write.
            _logger.exception(ex)
            self.stats["skipped"] += 1
            return {"should_link": False, "reason": "error", "domain": None}

    async def _company_for(self, domain: str, name: str):
        """Find the company for this domain, or create it.

        Looks up by the SAME normalised domain a hand-typed company is stored
        under, so this never creates a second copy of a company the account
        already has.
        """
        if domain in self._cache:
            return self._cache[domain]

        normalised = dedupe.normalize_domain(domain) or domain
        found = await self.companies.list_by(
            {"created_by": self.account_id, "domain": normalised,
             "is_archived": False}, limit=1)
        if found:
            self._cache[domain] = found[0]
            return found[0]

        company = CompanyModel(
            created_by=self.account_id,
            created_by_user_id=self.actor_user_id,
            owner_id=self.actor_user_id,
            domain=normalised,
            # `source` is what tells a user why a company they never made is in
            # their list, and is how they find them all to clean up if they
            # decide they do not want this.
            source="auto_domain",
            **dedupe.derive_name_fields(name or normalised),
        )
        created = await self.companies.insert(company)
        if created is None:
            return None
        self.stats["created"] += 1
        self._cache[domain] = created
        return created


async def link_one(*, account_id: Any, contact_id: Any, email: Optional[str],
                   companies, associations, settings, actor_user_id: Any = None,
                   existing_domains: Optional[List[str]] = None
                   ) -> Dict[str, Any]:
    """Convenience for the single-contact paths."""
    linker = AutoLinker(account_id=account_id, companies=companies,
                        associations=associations, settings=settings,
                        actor_user_id=actor_user_id)
    return await linker.link_contact(contact_id, email, existing_domains)
