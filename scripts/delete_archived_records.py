#!/usr/bin/env python3
"""Remove every archived company and contact. Archiving was retired 29 Oct 2026.

    python3 scripts/delete_archived_records.py --dry-run
    python3 scripts/delete_archived_records.py --apply [--account <id>]

The archive action is gone from the product: a delete is now permanent for
every CRM object. The rows archived before that change are invisible — no list
shows them and no action can reach them — so they are deleted here.

⚠️ **IRREVERSIBLE. RUN `--dry-run` FIRST AND READ THE COUNTS.**

────────────────────────────────────────────────────────────────────────────
MERGE LOSERS ARE SKIPPED, AND THAT IS THE WHOLE REASON THIS SCRIPT IS NOT A
TWO-LINE `delete_many`
────────────────────────────────────────────────────────────────────────────
`merge_service` archives the LOSER of a company merge and keeps its
`merge_snapshot`, which is what makes an unmerge able to put back exactly what
the merge took. Those rows are archived but they are **live data** — deleting
them silently destroys every unmerge still inside its 30-day window and leaves
`merged_into_id` pointers resolving to nothing.

So a record with `merged_into_id` set is reported and left alone. If you also
want those gone, that is a separate decision about abandoning unmerge, and it
should be taken with the window length in front of you — not inside a cleanup.

Associations are removed with each record, because an association to a deleted
company is a link from a live deal to an id that resolves to nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGETS = (
    ("companies", "company"),
    ("crm_contacts", "contact"),
    ("crm_contact_profiles", "contact"),
)


async def run(apply: bool, account: Optional[str]) -> int:
    from app.config import settings                                # noqa: F401
    from app.db import init_db, close_db

    db = await init_db()
    if db is None:
        print("could not connect to the database", file=sys.stderr)
        return 2

    from app.object_id import PyObjectId

    scope: Dict[str, Any] = {}
    if account:
        scope["created_by"] = PyObjectId(str(account))

    print(f"\n{'APPLY' if apply else 'DRY RUN — nothing written'}"
          f"{f' · account {account}' if account else ' · every account'}\n")

    grand_deleted = grand_kept = 0
    for collection, entity_type in TARGETS:
        q = dict(scope)
        q["is_archived"] = True

        archived = await db[collection].count_documents(q)
        if not archived:
            print(f"  {collection:24} nothing archived")
            continue

        # Merge losers: archived, but live data an unmerge still needs.
        protected_q = dict(q)
        protected_q["merged_into_id"] = {"$ne": None}
        protected = await db[collection].count_documents(protected_q)

        deletable_q = dict(q)
        deletable_q["$or"] = [{"merged_into_id": None},
                              {"merged_into_id": {"$exists": False}}]

        ids = [row["_id"] async for row
               in db[collection].find(deletable_q, {"_id": 1})]

        print(f"  {collection:24} {len(ids):>6} to delete"
              + (f"   ({protected} merge loser(s) KEPT)" if protected else ""))
        grand_deleted += len(ids)
        grand_kept += protected

        if not apply or not ids:
            continue

        # Associations first: a link outliving its record is worse than an
        # orphan row, because a live deal renders it.
        for record_id in ids:
            await db["crm_associations"].delete_many({
                "$or": [
                    {"from_type": entity_type, "from_id": record_id},
                    {"to_type": entity_type, "to_id": record_id},
                    # crm_contacts rows also appear under the legacy type.
                    {"from_type": "crm_contact", "from_id": record_id},
                    {"to_type": "crm_contact", "to_id": record_id},
                ]})
        res = await db[collection].delete_many({"_id": {"$in": ids}})
        print(f"  {'':24} {getattr(res, 'deleted_count', 0):>6} deleted")

    verb = "deleted" if apply else "would be deleted"
    print(f"\n  {grand_deleted} record(s) {verb}.")
    if grand_kept:
        print(f"  {grand_kept} merge loser(s) kept — see this script's header.")
    if not apply:
        print("\n  Re-run with --apply to perform it. This cannot be undone.\n")
    else:
        print()
    await close_db()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="report what would be deleted and write nothing")
    g.add_argument("--apply", action="store_true",
                   help="delete them. Irreversible.")
    ap.add_argument("--account", default=None, help="limit to one account id")
    args = ap.parse_args()
    return asyncio.run(run(apply=args.apply, account=args.account))


if __name__ == "__main__":
    raise SystemExit(main())
