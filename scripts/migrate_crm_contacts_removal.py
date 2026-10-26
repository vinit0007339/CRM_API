#!/usr/bin/env python3
"""Report, and optionally repoint, what the CRM contact removal left behind.

    python3 scripts/migrate_crm_contacts_removal.py            # report only
    python3 scripts/migrate_crm_contacts_removal.py --apply    # repoint

⚠️ READ-ONLY BY DEFAULT, AND IT DELETES NOTHING EVER. Even with ``--apply`` the
only writes are to ``crm_notes.contact_ids`` and ``crm_tasks.contact_ids``, and
only to replace a now-dangling CRM contact id with the matching PLATFORM
contact id. The `crm_contacts` collection and any contact attribute rows are
left exactly where they are: this change removed a feature, and a migration
that also destroys the data makes the decision irreversible. Dropping the
collection is a separate, deliberate act for whoever is sure.

WHAT HAPPENED. The CRM's own people object (`crm_contacts`) was removed; the
app has one contacts list, the marketing audience. Three kinds of row refer to
something that no longer exists:

1. ``crm_notes.contact_ids`` / ``crm_tasks.contact_ids`` held CRM contact ids.
   The arms survive and now mean PLATFORM contact ids — the same thing an
   association's contact side, `EntityPicker` and a company's Contacts tab
   point at. A row left alone points at nothing, so the note or task silently
   stops appearing on that person.
2. ``crm_attributes`` rows with ``object_type: "contact"`` — definitions for a
   type nothing reads. Reported, never touched.
3. ``crm_saved_views`` and ``crm_view_preferences`` with
   ``object_type: "contact"`` — a saved view for a screen that no longer
   exists. Reported, never touched.

HOW THE REPOINT WORKS, and where it cannot. A CRM contact is matched to a
platform contact **by email, within the same account**, because that is the only
identifier the two collections share. Matching is case-insensitive:
``Contact.email`` is deliberately not lowercased on write (it carries a unique
index and folding case would collide existing rows), while `crm_contacts` stored
it normalised.

Three cases cannot be repointed and are listed individually rather than
summarised, because each needs a human decision:

* the CRM contact had **no email** (a phone-only lead) — nothing to match on;
* **no platform contact** with that address is visible to the account — the
  person was never in the marketing audience;
* the CRM contact row is **already gone**, so the id cannot even be looked up.

For those, the note or task keeps its dangling id. It is invisible on the
person's timeline but the row and its body are intact, which is the recoverable
failure rather than the lossy one.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, ".")

from app.config import logger as core_app_logger  # noqa: E402
from app.db import Database  # noqa: E402

CRM_CONTACTS = "crm_contacts"
PLATFORM_CONTACTS = "contacts"
NOTES = "crm_notes"
TASKS = "crm_tasks"
ATTRIBUTES = "crm_attributes"
SAVED_VIEWS = "crm_saved_views"
VIEW_PREFS = "crm_view_preferences"


async def platform_id_for(db, account_id: Any, email: Optional[str]):
    """The account's platform contact with that address, or None.

    Scoped on ``created_by`` OR ``owner_ids`` — the same predicate
    ``api/services/contact_scope.py`` builds, and both halves are needed:
    `created_by` is who made the row, `owner_ids` everyone since granted it.
    """
    if not email:
        return None
    pattern = {"$regex": f"^{re.escape(str(email).strip())}$", "$options": "i"}
    row = await db[PLATFORM_CONTACTS].find_one({
        "email": pattern,
        "$or": [{"created_by": account_id}, {"owner_ids": account_id}],
    }, {"_id": 1})
    return row["_id"] if row else None


async def crm_contact_index(db) -> Dict[Any, Tuple[Any, Optional[str]]]:
    """``{crm_contact_id: (account_id, email)}`` for every surviving row."""
    index: Dict[Any, Tuple[Any, Optional[str]]] = {}
    async for row in db[CRM_CONTACTS].find({}, {"_id": 1, "created_by": 1,
                                                "email": 1}):
        index[row["_id"]] = (row.get("created_by"), row.get("email"))
    return index


async def scan(db, collection: str, index) -> Tuple[List[dict], Counter]:
    """Rows needing attention in one collection, with a reason tally."""
    todo: List[dict] = []
    tally: Counter = Counter()
    async for row in db[collection].find(
            {"contact_ids": {"$exists": True, "$ne": []}},
            {"_id": 1, "created_by": 1, "contact_ids": 1}):
        account = row.get("created_by")
        mapping: Dict[Any, Any] = {}
        for old in row.get("contact_ids") or []:
            known = index.get(old)
            if known is None:
                tally["crm contact row already gone"] += 1
                continue
            _, email = known
            if not email:
                tally["crm contact had no email"] += 1
                continue
            new = await platform_id_for(db, account, email)
            if new is None:
                tally["no platform contact for that address"] += 1
                continue
            mapping[old] = new
            tally["repointable"] += 1
        if mapping:
            todo.append({"_id": row["_id"], "account": account,
                         "mapping": mapping})
    return todo, tally


async def count_by_object_type(db, collection: str, object_type: str) -> int:
    try:
        return await db[collection].count_documents(
            {"object_type": object_type})
    except Exception as ex:  # pylint: disable=broad-except
        _logger.warning(f"{collection}: {ex}")
        return -1


async def main(apply: bool) -> int:
    await Database.connect()
    db = Database.db

    index = await crm_contact_index(db)
    print(f"crm_contacts rows still present: {len(index):,}")
    print("  (left in place on purpose — this script deletes nothing)\n")

    total_repointed = 0
    for collection in (NOTES, TASKS):
        todo, tally = await scan(db, collection, index)
        print(f"{collection}: {len(todo):,} row(s) with a repointable contact")
        for reason, n in sorted(tally.items()):
            print(f"    {reason}: {n:,}")
        if apply and todo:
            for row in todo:
                new_ids = [row["mapping"].get(old, old)
                           for old in await current_ids(db, collection, row["_id"])]
                await db[collection].update_one(
                    {"_id": row["_id"]}, {"$set": {"contact_ids": new_ids}})
                total_repointed += 1
            print(f"    → repointed {len(todo):,} row(s)")
        print()

    for collection, label in ((ATTRIBUTES, "attribute definitions"),
                              (SAVED_VIEWS, "saved views"),
                              (VIEW_PREFS, "column layouts")):
        n = await count_by_object_type(db, collection, "contact")
        print(f"{collection}: {n:,} contact {label} — reported, NOT touched")

    print()
    if apply:
        print(f"APPLIED. {total_repointed:,} row(s) repointed.")
    else:
        print("REPORT ONLY. Re-run with --apply to repoint notes and tasks.")
    return 0


async def current_ids(db, collection: str, row_id) -> List[Any]:
    """Re-read the row's ids immediately before writing.

    The scan and the write are separate passes, and somebody may have edited
    the note in between. Re-reading means a concurrent edit is preserved
    instead of being overwritten with the list this script first saw.
    """
    row = await db[collection].find_one({"_id": row_id}, {"contact_ids": 1})
    return list((row or {}).get("contact_ids") or [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Repoint notes and tasks (default: report only)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
