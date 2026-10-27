#!/usr/bin/env python3
"""Is `crm_contacts` carrying the OLD unique index? Run before deploying.

    python3 scripts/check_crm_contact_index.py            # report only
    python3 scripts/check_crm_contact_index.py --apply    # drop the stale index

⚠️ WHY THIS EXISTS, AND WHY IT IS NOT OPTIONAL. `app/models/contact.py`
declares `uniq_crm_contact_email` as a PARTIAL unique index. The version that
shipped before the contacts object was removed declared the same name with
`sparse=True`. Mongo refuses to create an index whose name already exists with
different options — `IndexOptionsConflict`, error 85 — and `app/db.py` sets
`allow_index_dropping=False`, deliberately, so Beanie will not resolve the
clash for us.

`crm_contacts` was never dropped: `scripts/migrate_crm_contacts_removal.py`
destroyed nothing on purpose, so any database that ever carried the object
still has the old index on it.

WHAT THAT LOOKS LIKE IF YOU SKIP THIS. The process starts — `init_beanie`
failures are logged and surfaced through readiness rather than crashing, which
is the lifespan's whole design — and then `/health/ready` reports
`models.registered: false` for ever. Every CRM request fails on a model that
never registered, and the error naming the real cause is one line in the log,
at startup, above whatever you are actually looking at.

WHY THE OLD INDEX WAS WRONG, since dropping an index deserves a reason. It was
meant to let a contact have no email — a phone-only lead. On a COMPOUND index
`sparse` does not do that: Mongo's rule is that a compound sparse index only
indexes documents that contain a value for at least ONE of the keys, and
`created_by` is always present. So every document was indexed, a missing email
stored as null, and the SECOND phone-only contact in an account collided on
`(account, null)`. `sparse` would have worked on a single-field index on
`email` alone, which is the form the MongoDB manual demonstrates.

⚠️ IT DROPS AN INDEX, NEVER A DOCUMENT. `--apply` issues exactly one
`drop_index`, and only when the index exists AND its options differ from what
the model declares AND the collection has no duplicate that would stop the new
one being built. Nothing else is written. Re-creating the index is Beanie's job
at the next startup — this script does not create it, so a mistake here costs a
restart rather than data.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INDEX = "uniq_crm_contact_email"
COLLECTION = "crm_contacts"


def declared_options() -> dict:
    """What `app/models/contact.py` says the index should be."""
    from app.models.contact import CrmContactModel

    for model_index in CrmContactModel.Settings.indexes:
        document = dict(model_index.document)
        if document.get("name") == INDEX:
            return document
    print(f"✗ {INDEX} is not declared in app/models/contact.py — this script "
          f"is checking for an index the model no longer wants. Either the "
          f"model changed or this script is stale; do not --apply.")
    sys.exit(1)


def comparable(options: dict) -> dict:
    """The options that decide whether Mongo considers two indexes the same.

    `ns`, `v` and the key order's representation differ between server versions
    and between a live index and a declared one, so comparing raw documents
    reports a conflict that is not there.
    """
    keep = ("unique", "sparse", "partialFilterExpression", "expireAfterSeconds")
    out = {k: options[k] for k in keep if k in options}
    key = options.get("key")
    if key is not None:
        out["key"] = list(dict(key).items()) if not isinstance(key, list) \
            else [tuple(pair) for pair in key]
    return out


async def run(apply: bool) -> int:
    from app.config import settings
    from app.db import Database, init_db

    if not settings.db_uri or not settings.db_name:
        print("✗ no database configured — set [MAIN_DATABASE] db_uri/db_name "
              "or MAIN_DB_URI/MAIN_DB_NAME. Nothing checked.")
        return 1

    # Connect WITHOUT registering models: `init_db` would run the very index
    # creation this script exists to make safe.
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(settings.db_uri, serverSelectionTimeoutMS=5000)
    database = client[settings.db_name]

    try:
        await database.command({"ping": 1})
    except Exception as ex:                      # pylint: disable=broad-except
        print(f"✗ cannot reach {settings.db_name}: {type(ex).__name__}: {ex}")
        return 1

    names = await database.list_collection_names()
    if COLLECTION not in names:
        print(f"✓ `{COLLECTION}` does not exist in {settings.db_name} — this "
              f"database never carried the object, so there is no stale index "
              f"and nothing to do. Beanie will create the declared index on "
              f"first startup.")
        return 0

    live = {ix["name"]: ix async for ix in database[COLLECTION].list_indexes()}
    count = await database[COLLECTION].count_documents({})
    print(f"`{COLLECTION}` exists: {count} document(s), "
          f"{len(live)} index(es)\n")

    if INDEX not in live:
        print(f"✓ `{INDEX}` is not present, so there is nothing to conflict "
              f"with. Beanie will create the declared index at startup.")
        return 0

    found = comparable(dict(live[INDEX]))
    wanted = comparable(declared_options())

    if found == wanted:
        print(f"✓ `{INDEX}` already matches what the model declares:\n"
              f"    {found}\n"
              f"  Nothing to do.")
        return 0

    print(f"✗ `{INDEX}` CONFLICTS with the model. Registering "
          f"CrmContactModel against this database will fail with "
          f"IndexOptionsConflict and readiness will report "
          f"`models.registered: false`.\n")
    print(f"  on the server : {found}")
    print(f"  model declares: {wanted}\n")

    # Would the new index even build? A partial unique index is narrower than
    # the old sparse one, so it can only fail on genuine duplicate non-null
    # emails within an account — which the old index should have prevented, but
    # a database that predates it, or one written to while it was absent, may
    # carry them. Finding out AFTER dropping the old index is the bad order.
    duplicates = await database[COLLECTION].aggregate([
        {"$match": {"email": {"$type": "string"}}},
        {"$group": {"_id": {"created_by": "$created_by", "email": "$email"},
                    "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$limit": 10},
    ]).to_list(length=10)

    if duplicates:
        print(f"  ⚠️ AND {len(duplicates)}+ duplicate (account, email) pair(s) "
              f"already exist, so the new unique index cannot build even once "
              f"the old one is gone. Resolve these FIRST — dropping the index "
              f"now would leave the collection with no uniqueness at all:")
        for row in duplicates:
            key = row["_id"]
            print(f"      account {key.get('created_by')} · "
                  f"{key.get('email')} × {row['n']}")
        print()
        return 1

    if not apply:
        print("  Re-run with --apply to drop it. The declared index is then "
              "created by Beanie at the next startup; this script does not "
              "create it.")
        return 1

    await database[COLLECTION].drop_index(INDEX)
    print(f"  dropped `{INDEX}`.\n"
          f"  ⚠️ THE COLLECTION NOW HAS NO EMAIL UNIQUENESS until the service "
          f"starts and Beanie creates the declared index. Restart it now, then "
          f"confirm with `/health/ready` that `models.registered` is true.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="drop the stale index (one drop_index, no writes "
                             "to documents)")
    args = parser.parse_args()
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
