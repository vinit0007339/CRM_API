"""Contact counts that reflect people who still exist. CRM-432.

⚠️ WHY THE STORED ROLLUP IS NOT ENOUGH. `company.contact_count` and
`deal.contact_count` are maintained by `crud/association._bump` on every link
and unlink, and `crud/association.py` says plainly what they are: display-only
counters that drift and are reconciled nightly. The drift this module exists
for is not nightly and does not reconcile — deleting a contact from the
marketing audience leaves this service's association row untouched, because
nothing on the platform side knows these collections exist. The counter keeps
counting a person who is gone, and a company whose only contact was deleted
lists "1" against an empty Contacts tab.

The detail endpoints fixed this by counting what they could resolve. A list
cannot resolve a page of records one at a time, so this does it in two reads
for the whole page regardless of its size: one pass over `crm_associations`,
one lookup of the ids it found.

⚠️ A LOOKUP THAT FAILED RETURNS `[]` EXACTLY LIKE ONE THAT FOUND NOTHING —
`host/directory.py` swallows the error by design — so "every contact on this
page is gone" and "the host is unreachable" can arrive identically. Whether
that ambiguity is real depends on WHICH ADAPTER is configured, and so does the
answer:

* `shared_db` (the default): the host's `contacts` live in the SAME database
  and the SAME session that just served the page of companies. A lookup
  through it cannot fail selectively — if it is down, the list request never
  got this far. So a miss means deleted, and is counted as such.
* `http` / `none`: the host is a separate service that fails on its own, and
  an outage there would zero every count on the page. That reads as data loss,
  so a WHOLESALE miss is treated as a failed read and the stored counter
  stands. A PARTIAL miss cannot be an outage and is counted.

⚠️ THE FIRST BRANCH IS NOT AN OPTIMISATION — IT IS THE WHOLE BUG. The case
that prompted this is one company whose ONLY contact was deleted, which is a
wholesale miss on its page. Treating that as an outage everywhere left exactly
that company still reading "1".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app import foreign
from app.config import logger as _logger
from app.host.directory import get_directory


async def contact_counts(
    *,
    assoc_crud: Any,
    contact_crud_factory: Any,
    db_session: Any,
    account_id: Any,
    entity_type: str,
    entity_ids: List[Any],
) -> Dict[str, Optional[int]]:
    """``{str(entity_id): live count}`` for the records on one page.

    An entity is absent from the result when its count could not be
    established, which the caller reads as "keep the stored counter".
    """
    out: Dict[str, Optional[int]] = {}
    if not entity_ids:
        return out
    try:
        links = await assoc_crud.person_links(
            account_id=account_id, entity_type=entity_type, entity_ids=entity_ids)
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return out

    # A record with no association rows has no people, full stop — there is
    # nothing to resolve and nothing that could have failed, so 0 is certain.
    for entity_id in entity_ids:
        out[str(entity_id)] = 0
    if not links:
        return out

    platform_ids = {i for _e, t, i in links if t == "contact"}
    crm_ids = {i for _e, t, i in links if t == "crm_contact"}

    # ⚠️ READ ONCE, BEFORE THE LOOKUPS. Which adapter is configured decides
    # whether an empty result is trustworthy; see the module docstring.
    try:
        host_is_local = get_directory().name == "shared_db"
    except Exception as ex:  # pylint: disable=broad-except
        # Unknowable means untrusting: behave as if the host were remote.
        _logger.exception(ex)
        host_is_local = False

    alive: set = set()
    if platform_ids:
        rows = await foreign.contacts_by_ids(db_session, list(platform_ids))
        found = {r.get("_id") for r in rows or []}
        if not found and not host_is_local:
            # Remote host, nothing came back: an outage and a mass deletion are
            # indistinguishable, and only one of them should rewrite the page.
            return {}
        alive |= found
    if crm_ids:
        try:
            crud = contact_crud_factory(db_session)
            rows = await crud.list_by(
                {"created_by": crud.oid(account_id), "_id": {"$in": list(crm_ids)}},
                limit=len(crm_ids))
            # ⚠️ NO ADAPTER BRANCH HERE. `crm_contacts` is this service's OWN
            # collection — there is no host and no boundary to be unreachable
            # across, so an empty result is an answer, not a maybe. The `except`
            # below is what covers a genuine failure.
            alive |= {getattr(r, "id", None) for r in rows or []}
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            return {}

    for entity_id, _person_type, person_id in links:
        if person_id in alive:
            key = str(entity_id)
            out[key] = (out.get(key) or 0) + 1
    return out
