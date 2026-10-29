"""The one door through which this service writes to a host collection.

Phase 5 of `docs/contact-unification-plan.md`. The write-side sibling of
`app/foreign.py`, and deliberately a SEPARATE file.

⚠️ **`foreign.py` SAYS THERE SHOULD NEVER BE A `write_*` IN IT, AND THERE
ISN'T.** That rule is kept. What changed is that the CRM now has a reason to
write at all: with one person shared between marketing and sales, creating a
contact in the CRM *is* creating a marketing contact. Putting those writes in
`foreign.py` would have made its count of host READS meaningless and let a
call site reach for a write while believing it was querying.

⚠️ **THE SAME ARGUMENT AS `foreign.py`, APPLIED TO WRITES: the call sites are
the thing that must not grow.** Three functions, and every one of them is a
request to the host — `app/host/writer.py` decides whether that request is an
insert into a shared database or an HTTP POST.

⚠️ **EVERY WRITE HERE IS ACCOUNT-SCOPED IN THE ADAPTER, NOT HERE.** The scope
lives in the update filter so a check-then-write race cannot land on another
tenant's person. See `SharedDbWriter.update_contact`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.host.writer import get_writer


async def create_contact(db: Any, account_id: Any,
                         fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Create a person in the marketing audience. Raises on a duplicate address.

    ⚠️ THE CALLER OWNS THE CONSENT DECISION AND MUST PASS IT EXPLICITLY.
    `Contact.subscribed` defaults to **True** on the model, so a create that
    simply omits it opts a sales lead into marketing — a consent decision made
    by forgetting. `contact_rules.new_person_fields` is what builds this dict,
    and it sets `subscribed=False`.
    """
    return await get_writer().create_contact(db, account_id, fields)


async def update_contact(db: Any, account_id: Any, contact_id: Any,
                         fields: Dict[str, Any]) -> bool:
    """Set person fields on a contact this account may write.

    False means nothing was written — either the row does not exist or it
    belongs to another account and this one only holds it by sharing. The
    caller decides what that means; silently succeeding is not an option.
    """
    return await get_writer().update_contact(db, account_id, contact_id, fields)


async def attach_owner(db: Any, account_id: Any, contact_id: Any) -> bool:
    """Grant this account a person that already exists.

    The answer to the global unique index on `Contact.email`: two accounts
    cannot each own a row for one address, so the second one joins `owner_ids`.
    """
    return await get_writer().attach_owner(db, account_id, contact_id)


async def find_or_create_contact(db: Any, account_id: Any,
                                 email: Optional[str],
                                 fields: Dict[str, Any]
                                 ) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Create the person, or adopt the one already holding that address.

    Returns `(row, created)`. `created=False` means the address was already in
    the audience — possibly another account's, which is legitimate: the row is
    shared through `owner_ids`, and this account has just been added to it.

    ⚠️ THE GLOBAL LOOKUP THIS IMPLIES LIVES IN THE ADAPTER, because it is
    forced by `Contact.email`'s global unique index rather than chosen. See
    `SharedDbWriter.find_or_create_contact` and `app/services/contact_scope.py`.
    """
    return await get_writer().find_or_create_contact(db, account_id, email, fields)
