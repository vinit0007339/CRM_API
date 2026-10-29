"""The three collections this service reads but does not own.

    users       owner expansion — the name beside a deal or company
    tags        a company's tag chips
    contacts    a company's Contacts tab, resolving a note's or task's
                target to a platform contact, and — since phase 4b — paging
                the audience itself for the unified "All contacts" view

⚠️ EVERY READ OF THEM GOES THROUGH THIS FILE. That is the whole point of it.
The CRM owns 14 collections and writes to no others; these three belong to the
host. Scatter `db["contacts"]` through 38,000 lines instead and integrating
this service into a second project starts with an archaeology phase.

That bet paid off. This file is now six one-line delegations to
`app/host/directory.py`, where the same six reads live behind an interface
with three implementations — one database shared with the host, one HTTP call
to it, or none at all. The call sites below did not change, and neither did
the 891 integration tests that exercise them.

⚠️ SO WHY KEEP THIS FILE AT ALL, rather than calling `get_directory()`
directly from the five call sites? Because the five call sites are the thing
that must not grow. A module-level function is cheap to call from a sixth
place; `foreign.py` existing as the single named door is what has kept the
host surface to six reads through three phases of this move — and the sixth
was argued for in a plan before it was written, which is the bar. The indirection is
the point, not an artefact.

There is no `write_*` function here, and there should never be one. A write to
a host collection is a request to the host, not a query. ⚠️ Since phase 5 of
the contact unification there ARE such requests — `app/host_writes.py`, over
`app/host/writer.py`. They live in their own file precisely so this rule can
stay true here: a call site reaching into `foreign` is reading, always, and
this module's count of host reads keeps meaning something.

⚠️ AND NO BEANIE MODEL FOR THEM. Registering the platform's `Contact`
Document here would let this service manage the `contacts` indexes — a
collection it does not own — which is exactly the ownership line this file
exists to draw. So these are raw reads returning dicts, and `ForeignRecord`
gives the one attribute the CRM code actually uses: `.id`.

EXPANSION IS COSMETIC, AND EVERY EXPANSION SWALLOWS ITS FAILURES. A missing
owner name must not fail a list of deals. A LOOKUP, though, returns None and
lets the caller report a row failure, because silently importing a note
against nobody is worse than refusing the row. Both rules are now stated once,
in `directory.py`, and every adapter has to honour them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.host.directory import ForeignRecord, get_directory

#: Re-exported so `from app.foreign import ForeignRecord` keeps working. It is
#: defined in `host/directory.py` because that is where the adapters build it.
__all__ = [
    "ForeignRecord", "contact_by_email", "contact_by_id", "contacts_by_ids",
    "users_by_ids", "tags_by_ids",
]


# ── contacts ────────────────────────────────────────────────────────────────

async def contact_by_email(db: Any, account_id: Any,
                           email: str) -> Optional[ForeignRecord]:
    """One account's contact with that address, or None. LOOKUP — scoped."""
    return await get_directory().contact_by_email(db, account_id, email)


async def contact_by_id(db: Any, account_id: Any,
                        contact_id: Any) -> Optional[ForeignRecord]:
    """One account's contact by id, or None. LOOKUP — scoped."""
    return await get_directory().contact_by_id(db, account_id, contact_id)


async def contacts_by_ids(db: Any,
                          contact_ids: List[Any]) -> List[Dict[str, Any]]:
    """Rows for a company's Contacts tab. EXPANSION — not scoped, see adapter."""
    return await get_directory().contacts_by_ids(db, contact_ids)


async def contacts_page(db: Any, account_id: Any, *, q: Optional[str] = None,
                        skip: int = 0, limit: int = 50,
                        sort: Optional[List[tuple]] = None
                        ) -> Tuple[List[Dict[str, Any]], int]:
    """A page of this account's marketing audience, and the true total.

    ⚠️ THE SIXTH DOOR. This file's whole argument is that five is the number
    and it must not grow, so this was added as a decision rather than reached
    for — `docs/contact-unification-phases.md` phase 4b. The unified "All
    contacts" view lists the audience itself, and the other five reads can
    only resolve ids the caller already holds; there is no way to page or
    search a collection this service does not own.

    ⚠️ A LOOKUP, NOT AN EXPANSION. It is account-scoped in every adapter, and
    an adapter that cannot answer returns `([], 0)` rather than raising — the
    All view goes empty, the default view is untouched.

    ⚠️ IF YOU ARE ABOUT TO ADD A SEVENTH: don't, without the same deliberate
    step. Each one is a thing the next host integration has to implement.
    """
    return await get_directory().contacts_page(
        db, account_id, q=q, skip=skip, limit=limit, sort=sort)


# ── users & tags: display expansion, never business logic ───────────────────

async def users_by_ids(db: Any, user_ids: List[Any]) -> List[Dict[str, Any]]:
    """One query for a whole page of rows, never one per row (NFR6)."""
    return await get_directory().users_by_ids(db, user_ids)


async def tags_by_ids(db: Any, tag_ids: List[Any]) -> List[Dict[str, Any]]:
    """EXPANSION. A failure is logged and returns []."""
    return await get_directory().tags_by_ids(db, tag_ids)
