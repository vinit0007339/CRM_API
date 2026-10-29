#!/usr/bin/env python3
"""Repair the identity cache on `crm_contact_profiles`.

Phase 4 of `docs/contact-unification-plan.md`. The profile carries
`full_name`, `email` and `job_title` so the unified contact list can sort and
search locally — you cannot sort across two collections, and `contacts` is
behind the `Directory` interface where a `$lookup` may not exist at all.

⚠️ **THE DRIFT IS ONE-DIRECTIONAL AND NOTHING TELLS US.** Marketing can rename
or re-address a contact at any time; the platform has never heard of this
collection, so there is no hook to fire. That is the trade accepted when the
cache was chosen over a sixth host read, and this script is the other half of
it. Run it nightly.

⚠️ **A STALE CACHE IS A MISSED SEARCH HIT, NEVER A WRONG RECORD.** Every id in
a profile stays authoritative, and the detail page renders name and email from
the person, not from here. So a late run degrades finding somebody; it cannot
attach one person's deals to another.

Usage:
    python3 scripts/reconcile_contact_profiles.py --dry-run
    python3 scripts/reconcile_contact_profiles.py --apply [--account <id>]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List

BATCH = 500


async def _run(apply: bool, account: str | None) -> int:
    from app.config import logger as _logger, settings          # noqa: F401
    from app.db import init_db, close_db
    from app import foreign
    from app.crud.contact_profile import CrmContactProfileCrud
    from app.services import contact_rules as cr

    db = await init_db()
    if db is None:
        print("could not connect to the database", file=sys.stderr)
        return 2

    crud = CrmContactProfileCrud(db)
    query: Dict[str, Any] = {}
    if account:
        query["created_by"] = crud.oid(account)

    scanned = changed = missing = 0
    cursor = db["crm_contact_profiles"].find(query).batch_size(BATCH)
    batch: List[Dict[str, Any]] = []

    async def flush(rows: List[Dict[str, Any]]) -> None:
        nonlocal changed, missing
        if not rows:
            return
        people = await foreign.contacts_by_ids(
            db, [r.get("contact_id") for r in rows])
        by_id = {str(p.get("_id")): p for p in people}
        for row in rows:
            person = by_id.get(str(row.get("contact_id")))
            if person is None:
                # ⚠️ REPORTED, NEVER REPAIRED HERE. A profile whose person is
                # gone is either a deleted contact or a bad migration, and
                # both want a human. Blanking the cache would hide it.
                missing += 1
                continue
            wanted = {
                "full_name": cr.display_name(person.get("first_name"),
                                             person.get("last_name"),
                                             person.get("email")),
                "email": person.get("email"),
                "job_title": person.get("job_title"),
            }
            if all(row.get(k) == v for k, v in wanted.items()):
                continue
            changed += 1
            if apply:
                await crud.refresh_identity(
                    account_id=row.get("created_by"),
                    contact_id=row.get("contact_id"), **wanted)

    async for row in cursor:
        scanned += 1
        batch.append(row)
        if len(batch) >= BATCH:
            await flush(batch)
            batch = []
    await flush(batch)

    verb = "updated" if apply else "would update"
    print(f"scanned {scanned} profile(s); {verb} {changed}; "
          f"{missing} with no person record")
    await close_db()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="report what would change and write nothing")
    g.add_argument("--apply", action="store_true", help="write the repairs")
    ap.add_argument("--account", default=None,
                    help="limit to one account id")
    args = ap.parse_args()
    return asyncio.run(_run(apply=args.apply, account=args.account))


if __name__ == "__main__":
    raise SystemExit(main())
