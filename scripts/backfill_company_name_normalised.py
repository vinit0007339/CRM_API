"""Fill in `companies.name_normalised`. CRM-238.

Duplicate detection matches on this field. Rows written before it existed have
it null, and until it is filled they are found only by the fallback arm of the
candidate query — a substring search that misses exactly the pairs name-based
detection is for ("Acme Global Ltd" vs "Acme & Global Limited": same normalised
form, neither contains the other).

So the field is not optional in practice, and the failure when it is missing is
silent: no warning appears, and nobody reports a warning they never saw.

    python3 scripts/backfill_company_name_normalised.py --dry-run
    python3 scripts/backfill_company_name_normalised.py

`--dry-run` reports how many rows are wrong without writing, and exits non-zero
if any are, so it can be a check rather than something someone has to read.

Re-runnable: it recomputes every row and writes only the ones that disagree, so
it doubles as a drift check for the write paths.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Dict, Optional

sys.path.insert(0, ".")

from app.db import close_db, init_db                              # noqa: E402
from app.models.company import CompanyModel           # noqa: E402
from app.object_id import PyObjectId            # noqa: E402

from app.services import dedupe                        # noqa: E402


async def backfill(*, account: Optional[str] = None, dry_run: bool = False,
                   verbose: bool = False, collection=None) -> Dict[str, int]:
    """Compare the STORED value against the derived one, and fix what differs.

    ⚠️ Reads raw documents, not model instances. ``CompanyModel`` re-derives
    ``name_normalised`` in a validator whenever a document is loaded, so a row
    read through the model always *looks* correct — the value is healed in
    memory and never written back. A version of this script that iterated
    ``CompanyModel.find()`` would report zero wrong rows on a collection that
    was entirely wrong, and the queries that actually match on the field would
    keep missing them.
    """
    query = {"created_by": PyObjectId(account)} if account else {}
    stats = {"checked": 0, "wrong": 0, "fixed": 0}

    handle = collection if collection is not None else CompanyModel.get_motor_collection()

    for document in await handle.find(query).to_list():
        stats["checked"] += 1
        expected = dedupe.derive_name_fields(document.get("name"))["name_normalised"]
        if document.get("name_normalised") == expected:
            continue

        stats["wrong"] += 1
        if verbose or dry_run:
            print(f"  {document.get('_id')} {document.get('name')!r} "
                  f"{document.get('name_normalised')!r} -> {expected!r}")
        if dry_run:
            continue

        await handle.update_one({"_id": document["_id"]},
                                {"$set": {"name_normalised": expected}})
        stats["fixed"] += 1
    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="one account only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    await init_db()
    try:
        stats = await backfill(account=args.account, dry_run=args.dry_run,
                               verbose=args.verbose)
    finally:
        await close_db()

    verb = "would fix" if args.dry_run else "fixed"
    print(f"checked {stats['checked']} companies · "
          f"{stats['wrong']} wrong · {verb} {stats['fixed']}")
    return 1 if (args.dry_run and stats["wrong"]) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
