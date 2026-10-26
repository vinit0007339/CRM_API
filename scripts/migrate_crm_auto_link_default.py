"""Turn auto-linking OFF for accounts that existed before it shipped. CRM-242.

Q6, decided 2026-08-15: **on for new accounts, off for existing ones.**

`crm_settings.auto_link_contacts_by_domain` has defaulted to `True` since the
field was added, but nothing ever read it. The moment CRM-242 ships, every
account that already has settings would begin auto-creating companies on the
next contact edit — an account with 50,000 contacts could gain thousands of
companies without anybody choosing that, and the first they would know is a
company list that no longer resembles their business.

So this runs ONCE, at deploy, before the feature is enabled: every settings
document that exists today is set to False. New accounts, whose settings are
created after this has run, keep the `True` default and get the feature.

    python3 scripts/migrate_crm_auto_link_default.py --dry-run
    python3 scripts/migrate_crm_auto_link_default.py

⚠️ **Not re-runnable in spirit.** Running it a second time, later, would switch
off accounts that had deliberately turned it ON. `--only-untouched` (the
default) protects against that by skipping any account whose settings have been
updated since the field was introduced; pass `--all` to force.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Dict, Optional

sys.path.insert(0, ".")

# ⚠️ THE DB LIFECYCLE IS THE SERVICE'S NOW. In the monolith these
# scripts called `app.init_app_db` / `close_app_db`, which also
# registered every platform model. Here it is `app.db.init_db`, which
# registers the 14 CRM documents and nothing else — a script that
# cannot touch the platform's collections cannot migrate them by
# accident.
from app.db import close_db, init_db                         # noqa: E402
from app.models.settings import CrmSettingsModel      # noqa: E402
from app.object_id import PyObjectId            # noqa: E402


async def migrate(*, account: Optional[str] = None, dry_run: bool = False,
                  force: bool = False, collection=None) -> Dict[str, int]:
    """Set auto_link_contacts_by_domain = False on existing settings docs."""
    query: Dict = {"auto_link_contacts_by_domain": {"$ne": False}}
    if account:
        query["created_by"] = PyObjectId(account)

    handle = collection if collection is not None else \
        CrmSettingsModel.get_motor_collection()

    stats = {"checked": 0, "changed": 0, "skipped_opted_in": 0}
    for document in await handle.find(query).to_list():
        stats["checked"] += 1

        # An account that has already turned it on deliberately keeps it on.
        # Without this, a second run of the migration silently undoes the
        # user's own choice — and they would have no idea why linking stopped.
        if not force and document.get("auto_link_opted_in"):
            stats["skipped_opted_in"] += 1
            continue

        if dry_run:
            stats["changed"] += 1
            continue

        await handle.update_one(
            {"_id": document["_id"]},
            {"$set": {"auto_link_contacts_by_domain": False}})
        stats["changed"] += 1
    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="one account only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--all", action="store_true",
                        help="also switch off accounts that opted in")
    args = parser.parse_args()

    await init_db()
    try:
        stats = await migrate(account=args.account, dry_run=args.dry_run,
                              force=args.all)
    finally:
        await close_db()

    verb = "would switch off" if args.dry_run else "switched off"
    print(f"checked {stats['checked']} accounts · {verb} {stats['changed']}"
          f" · left {stats['skipped_opted_in']} that opted in")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
