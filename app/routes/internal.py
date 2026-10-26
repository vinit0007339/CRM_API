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
from app.crud import CompanyCrud, CrmAssociationCrud, CrmSettingsCrud
from app.crud.dependency import get_crud
from app.errors import AuthError
from app.schemas.common import ResponseSchema
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
