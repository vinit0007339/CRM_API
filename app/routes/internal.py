"""The platform's way in. One endpoint, and it should stay that way.

The CRM reaches the platform for identity (`app/auth/introspect.py`). This is
the other direction: the one thing the platform needs from the CRM.

WHAT IT IS FOR. Creating a contact, or importing a list of them, auto-creates
and links a CRM company from the email domain. That code moved here with the
rest of the CRM, so the platform now asks for it over HTTP —
`api/services/crm_client.py` on its side.

⚠️ IT TAKES A BATCH, AND THE BATCH IS THE CONTRACT, NOT AN OPTIMISATION. The
list importer's comment in the platform says why: "five thousand contacts at
the same company must be one company lookup, not five thousand — and must not
race itself into creating the company twice." `AutoLinker` caches per instance
to do exactly that, so this endpoint builds ONE linker for the whole request.
A single-contact endpoint called in a loop would undo it twice over: five
thousand HTTP requests, and five thousand cold caches racing to create the
same company.

⚠️ NOT UNDER `/crm`, AND THAT IS DELIBERATE. nginx routes `/api/v1/crm/` here
from the public hostname; this sits under `/api/v1/internal/` with the same
deny rule the platform's own internal endpoint has. Two reasons: it is not
part of the CRM's public contract (`scripts/check_crm_contract.py` counts
`/crm` only, so the documented 83 is unaffected), and it must never be
reachable from a browser — it names an account id and acts on it, with no
end-user token anywhere in the request.

WHY IT TRUSTS AN ACCOUNT ID AT ALL. Every other endpoint in this service
derives the account from a verified token. This one is called by the platform
on behalf of a user whose contact it has just written, after its own
`require("contacts:write")` has run — so the authorisation already happened,
one service earlier. What stands in for the token here is the shared secret
plus the network rule, which is why both are mandatory and why an unset secret
refuses the request rather than running open.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, Field

from app.config import logger, settings
from app.crud import (CompanyCrud, CrmAssociationCrud, CrmSettingsCrud,
                      DealCrud, PipelineCrud)
from app.crud.contact import CrmContactCrud
from app.crud.dependency import get_crud
from app.errors import AuthError
from app.models.deal import DealModel
from app.schemas.common import ResponseSchema
from app.services import dedupe
from app.services.auto_link_service import AutoLinker

router = APIRouter()


class _Contact(BaseModel):
    contact_id: str
    email: str


class AutoLinkRequest(BaseModel):
    account_id: str
    actor_user_id: Optional[str] = None
    #: A cap, because this is a batch endpoint reached by a shared secret: an
    #: unbounded list is an unbounded amount of work for one request. The
    #: platform's importer sends contacts in pages well under this.
    contacts: List[_Contact] = Field(default_factory=list, max_length=5000)


class _Lead(BaseModel):
    """One captured lead, as the platform has already stored it.

    `contact_id` is the PLATFORM contact's id — this endpoint does not copy the
    person into `crm_contacts`. See the note on `_link_lead`.
    """

    contact_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    company_name: Optional[str] = None
    #: Where the deal should land. Both optional: omitted means the account's
    #: default pipeline and its first open stage, which is what a new lead
    #: wants and what survives a stage being renamed.
    pipeline_id: Optional[str] = None
    stage_id: Optional[str] = None
    deal_name: Optional[str] = None
    owner_id: Optional[str] = None
    amount: Optional[float] = None
    custom_fields: Dict[str, Any] = Field(default_factory=dict)


class LeadRequest(BaseModel):
    account_id: str
    actor_user_id: Optional[str] = None
    #: The same cap and the same reason as `AutoLinkRequest`. A flow step sends
    #: one lead; the scheduler's first sweep of an existing list can send
    #: thousands, so the batch shape is not optional even though the common
    #: case is a single row.
    leads: List[_Lead] = Field(default_factory=list, max_length=5000)


async def _require_internal_key(x_internal_key: Optional[str] = Header(None)) -> None:
    """The shared secret. The nginx deny rule is the other half.

    ⚠️ AN UNSET SECRET REFUSES, rather than running open. An internal endpoint
    that is accidentally public tells you nothing — no error, no log line,
    just an account id anyone can act on. Failing closed makes the
    misconfiguration visible on the first call.

    `compare_digest`, not `==`: a plain comparison returns as soon as two bytes
    differ, and the time that takes leaks the secret one byte at a time.
    """
    import hmac

    expected = settings.internal_key
    if not expected:
        raise AuthError("Internal API is not configured.",
                        status.HTTP_503_SERVICE_UNAVAILABLE)
    if not x_internal_key or not hmac.compare_digest(str(x_internal_key),
                                                     str(expected)):
        # 404, not 403: the endpoint does not confirm its own existence to a
        # caller without the secret.
        raise AuthError("Not found.", status.HTTP_404_NOT_FOUND)


@router.post("/crm/auto-link", response_model=ResponseSchema,
             dependencies=[Depends(_require_internal_key)])
async def auto_link(
    payload: AutoLinkRequest,
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    associations: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    crm_settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Link a batch of platform contacts to CRM companies by email domain.

    ONE `AutoLinker` for the whole batch — see the note on batching above.
    Per-contact failures are counted, never raised: the platform has already
    written the contacts, and a company link that did not happen must not turn
    into an error on a request whose real work is done.
    """
    if not payload.contacts:
        return ResponseSchema(status="success", message="Nothing to link.",
                              data={"linked": 0, "created": 0, "skipped": 0,
                                    "failed": 0})

    linker = AutoLinker(
        account_id=payload.account_id,
        companies=companies,
        associations=associations,
        settings=crm_settings,
        actor_user_id=payload.actor_user_id,
    )

    failed = 0
    for contact in payload.contacts:
        try:
            await linker.link_contact(contact.contact_id, contact.email)
        except Exception as ex:  # pylint: disable=broad-except
            # ⚠️ ONE BAD ROW MUST NOT COST THE OTHER FOUR THOUSAND. The same
            # rule the import worker follows: count it, log it, carry on.
            failed += 1
            logger.warning("auto-link failed for %s: %s", contact.contact_id, ex)

    stats: Dict[str, Any] = dict(linker.stats)
    stats["failed"] = failed
    if stats.get("linked") or failed:
        logger.info("auto-link: %s", stats)
    return ResponseSchema(
        status="success",
        message=f"{stats.get('linked', 0)} contact(s) linked, "
                f"{stats.get('created', 0)} company(ies) created.",
        data=stats,
    )

async def _company_for_lead(lead: _Lead, *, account_id: Any, companies: CompanyCrud,
                            associations: CrmAssociationCrud,
                            crm_settings: CrmSettingsCrud,
                            actor_user_id: Any, linker: AutoLinker) -> Any:
    """The lead's company: by the name they typed, else by their email domain.

    ⚠️ THE TYPED NAME FIRST, AND THE DOMAIN ONLY AS A FALLBACK. A B2B enquiry
    form asks for the company, and what the person typed is better data than
    what their address implies — `AutoLinker`'s own free-provider blocklist
    means a gmail.com lead gets no company from the domain at all, which is
    most self-serve signups.

    ⚠️ MATCHED ON `name_normalised`, NOT ON `name`. That field is lowercased
    and legal-suffix-stripped, is written in exactly one place
    (`dedupe.derive_name_fields`), is re-derived by `CompanyModel`'s own
    validator, and is indexed — so "Acme", "Acme Ltd" and "ACME Limited" find
    the one company instead of creating three. Comparing raw names would create
    a company per spelling, which is the failure this endpoint would be blamed
    for within a week.
    """
    typed = (lead.company_name or "").strip()
    if typed:
        needle = dedupe.normalize_company_name(typed) or None
        if needle:
            found = await companies.list_by(
                {"created_by": companies.oid(account_id),
                 "name_normalised": needle, "is_archived": False}, limit=1)
            if found:
                return found[0]
        from app.models.company import CompanyModel

        company = CompanyModel(
            created_by=companies.oid(account_id),
            created_by_user_id=actor_user_id,
            owner_id=actor_user_id,
            # `source` is how a user understands why a company they never typed
            # is in their list, and how they find them all if they decide they
            # do not want this.
            source="automation",
            **dedupe.derive_name_fields(typed),
        )
        return await companies.insert(company)

    # No typed name: fall back to the domain linker, which already owns the
    # free-provider blocklist and the find-or-create-by-domain rule.
    if lead.email:
        try:
            await linker.link_contact(lead.contact_id, lead.email)
        except Exception as ex:                  # pylint: disable=broad-except
            logger.warning("lead intake: domain link failed for %s: %s",
                           lead.contact_id, ex)
    return None


async def _link_lead(lead: _Lead, *, account_id: Any, actor_user_id: Any,
                     companies: CompanyCrud, deals: DealCrud,
                     pipelines: PipelineCrud, associations: CrmAssociationCrud,
                     crm_settings: CrmSettingsCrud,
                     linker: AutoLinker) -> Dict[str, Any]:
    """Create the deal for one lead, and link it to the person and company.

    ⚠️ THE PERSON IS A PLATFORM CONTACT AND IS NOT COPIED. The association is
    written with `to_type="contact"`, which is what `POST /crm/deals` already
    does for its own `contact_ids` — `app/routes/companies.py` resolves both
    that type and `crm_contact` side by side, and its comments record why the
    backfill into `crm_contacts` was abandoned: the row says which collection
    its id belongs to, so nothing has to guess. Promotion to a CRM-owned person
    is a later, deliberate act, not something lead capture does silently.
    """
    # The same resolution `POST /crm/deals` does, and deliberately the same
    # calls rather than a second implementation of "which pipeline".
    pipeline = (await pipelines.get_scoped(account_id, lead.pipeline_id)
                if lead.pipeline_id
                else await pipelines.get_default_or_first(account_id=account_id))
    if pipeline is None:
        return {"contact_id": lead.contact_id, "error": "no pipeline"}

    stages = [s for s in (pipeline.stages or [])]
    stage = None
    if lead.stage_id:
        stage = next((s for s in stages if str(s.id) == str(lead.stage_id)), None)
    if stage is None:
        # Must stay in step with `stage_rules.first_open_stage`, which is the
        # same rule for the dict-shaped stages the deals route works with.
        # This one reads model objects, which is why it is not that call.
        # ⚠️ THE FIRST **OPEN** STAGE, not `stages[0]`. A pipeline's stages are
        # ordered and the won/lost ones are pinned last, but a consumer can
        # reorder them — and a lead created directly into Won would be counted
        # as a closed deal by every report on the board.
        stage = next((s for s in sorted(stages, key=lambda x: x.order or 0)
                      if str(getattr(getattr(s, "type", None), "value",
                                     getattr(s, "type", "open"))) == "open"), None)
    if stage is None:
        return {"contact_id": lead.contact_id, "error": "no open stage"}

    company = await _company_for_lead(
        lead, account_id=account_id, companies=companies,
        associations=associations, crm_settings=crm_settings,
        actor_user_id=actor_user_id, linker=linker)

    name = (lead.deal_name or "").strip() or (lead.name or "").strip() \
        or (lead.email or "").strip() or "New lead"

    deal = DealModel(
        created_by=deals.oid(account_id),
        created_by_user_id=actor_user_id,
        owner_id=deals.oid(lead.owner_id) if lead.owner_id else actor_user_id,
        name=name,
        pipeline_id=pipeline.id,
        stage_id=stage.id,
        amount=lead.amount,
        source="automation",
        custom_fields=dict(lead.custom_fields or {}),
        primary_company_id=getattr(company, "id", None),
        primary_company_name=getattr(company, "name", None),
    )
    created = await deals.insert(deal)
    if created is None:
        return {"contact_id": lead.contact_id, "error": "deal not created"}

    # ⚠️ THROUGH `normalize_pair`, NEVER A HAND-WRITTEN ROW. `AssociationEntity`
    # member order defines the canonical storage direction, and a row inserted
    # the wrong way round is a duplicate the unique index cannot catch.
    await associations.link(account_id=account_id,
                            from_type="deal", from_id=created.id,
                            to_type="contact", to_id=lead.contact_id,
                            actor_user_id=actor_user_id)
    if company is not None:
        # ⚠️ THE PRIMARY IS LINKED TOO, NOT JUST DENORMALISED — the same trap
        # `create_deal` documents. `primary_company_id` is only what the board
        # card reads; the ASSOCIATION is what `GET /deals/{id}/companies`, the
        # company's Deals tab and the rollup counters all work from. A deal
        # carrying only the denormalised pair shows a company in its header and
        # an empty Companies list.
        await associations.link(account_id=account_id,
                                from_type="company", from_id=company.id,
                                to_type="deal", to_id=created.id,
                                actor_user_id=actor_user_id)
    return {"contact_id": lead.contact_id, "deal_id": str(created.id),
            "company_id": str(company.id) if company is not None else None,
            "pipeline_id": str(pipeline.id), "stage_id": str(stage.id)}


@router.post("/crm/leads", response_model=ResponseSchema,
             dependencies=[Depends(_require_internal_key)])
async def capture_leads(
    payload: LeadRequest,
    companies: CompanyCrud = Depends(get_crud(CompanyCrud)),
    deals: DealCrud = Depends(get_crud(DealCrud)),
    pipelines: PipelineCrud = Depends(get_crud(PipelineCrud)),
    associations: CrmAssociationCrud = Depends(get_crud(CrmAssociationCrud)),
    crm_settings: CrmSettingsCrud = Depends(get_crud(CrmSettingsCrud)),
):
    """Create a deal per captured lead. Called by the platform, never a browser.

    ⚠️ PER-LEAD FAILURES ARE COUNTED, NEVER RAISED. The platform has already
    stored the contact by the time it calls this — its own request's real work
    is done — so a deal that could not be created must not turn into an error
    on a signup form. The same rule `/crm/auto-link` follows, and the same
    reason.
    """
    if not payload.leads:
        return ResponseSchema(status="success", message="Nothing to capture.",
                              data={"created": 0, "failed": 0, "results": []})

    # ONE linker for the whole batch: it caches company lookups per instance,
    # which is what stops five thousand leads at the same domain becoming five
    # thousand lookups racing to create the same company.
    linker = AutoLinker(
        account_id=payload.account_id,
        companies=companies,
        associations=associations,
        settings=crm_settings,
        actor_user_id=payload.actor_user_id,
    )

    results: List[Dict[str, Any]] = []
    failed = 0
    for lead in payload.leads:
        try:
            outcome = await _link_lead(
                lead, account_id=payload.account_id,
                actor_user_id=payload.actor_user_id,
                companies=companies, deals=deals, pipelines=pipelines,
                associations=associations, crm_settings=crm_settings,
                linker=linker)
        except Exception as ex:                  # pylint: disable=broad-except
            failed += 1
            logger.warning("lead intake failed for %s: %s", lead.contact_id, ex)
            outcome = {"contact_id": lead.contact_id, "error": "exception"}
        if outcome.get("error"):
            failed += 1 if "exception" not in outcome.get("error", "") else 0
        results.append(outcome)

    created = sum(1 for r in results if r.get("deal_id"))
    if created or failed:
        logger.info("lead intake: %s created, %s failed", created, failed)
    return ResponseSchema(
        status="success",
        message=f"{created} deal(s) created.",
        data={"created": created, "failed": failed, "results": results},
    )
