#!/usr/bin/env python3
"""Is the contact unification finished? Run before phase 9 removes anything.

    python3 scripts/check_unification_complete.py
    python3 scripts/check_unification_complete.py --account <id>

Phase 9 of `docs/contact-unification-plan.md` deletes `CrmContactModel`, its
CRUD, and eventually the `crm_contacts` collection. **That is the only
irreversible step in the plan**, and the rollback it removes is the untouched
collection itself. This script is the gate.

⚠️ **EXIT 0 MEANS "NOTHING STILL DEPENDS ON `crm_contacts`", NOT "GO".** It
proves the data is migrated and nothing points at the old id space. It cannot
prove the new path has been *lived with* — that is a judgement about how long
accounts have been running unified without complaints, and it belongs to a
person.

⚠️ **IT READS AND REPORTS. IT WRITES NOTHING, EVER.** A check that could
repair what it is checking is a check nobody trusts.

Five questions, each one a way phase 9 could destroy something:

  1. Is every account on the unified path?         flag still off somewhere
  2. Is every `crm_contacts` row migrated?         unmapped people
  3. Any association still `to_type="crm_contact"`? links that would orphan
  4. Any task or note holding a `crm_contacts` id? opaque arrays, silent loss
  5. Any profile whose person has vanished?        a migration that half-ran
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TICK, CROSS, WARN = "✓", "✗", "!"


async def run(account: Optional[str]) -> int:
    from app.config import settings
    from app.db import init_db, close_db
    from app.services import contact_scope

    db = await init_db()
    if db is None:
        print("could not connect to the database", file=sys.stderr)
        return 2

    from app.object_id import PyObjectId

    scope: Dict[str, Any] = {}
    if account:
        scope["created_by"] = PyObjectId(str(account))

    failures: List[str] = []
    notes: List[str] = []

    # ── 1. every account on the unified path ────────────────────────────────
    override = contact_scope.unified_override()
    if override == "on":
        print(f"  {TICK} every account is forced unified by the environment")
        notes.append(
            "The env override is doing the work. Phase 9 removes BOTH switches, "
            "so confirm each account's own flag is true as well — otherwise "
            "removing the override silently reverts them.")
    elif override == "off":
        failures.append(
            "the environment kill switch is ON (off), so every account is on "
            "the LEGACY path right now. Phase 9 would delete the code they are "
            "running.")
    else:
        q = dict(scope)
        q["unified_contacts"] = {"$ne": True}
        stragglers = await db["crm_settings"].count_documents(q)
        if stragglers:
            failures.append(
                f"{stragglers} account(s) still have unified_contacts false. "
                f"They are reading `crm_contacts` today.")
        else:
            print(f"  {TICK} every account has unified_contacts = true")

    # ── 2. every crm_contacts row migrated ──────────────────────────────────
    q = dict(scope)
    q["migrated_contact_id"] = {"$exists": False}
    unmapped = await db["crm_contacts"].count_documents(q)
    total_crm = await db["crm_contacts"].count_documents(dict(scope))
    if unmapped:
        failures.append(
            f"{unmapped} of {total_crm} crm_contacts row(s) were never "
            f"migrated (no `migrated_contact_id`). Those people exist ONLY in "
            f"the collection phase 9 drops.")
    else:
        print(f"  {TICK} all {total_crm} crm_contacts row(s) carry a mapping")

    # ── 3. associations on the old id space ─────────────────────────────────
    q = dict(scope)
    q["to_type"] = "crm_contact"
    stale_assoc = await db["crm_associations"].count_documents(q)
    if stale_assoc:
        failures.append(
            f"{stale_assoc} association(s) still point at to_type="
            f"'crm_contact'. Dropping the collection orphans them — the deal "
            f"or company keeps a link to an id that resolves to nothing.")
    else:
        print(f"  {TICK} no association points at the old id space")

    # ── 4. the opaque leaf arrays ───────────────────────────────────────────
    # ⚠️ THE QUIETEST FAILURE IN THE PLAN. `crm_tasks.contact_ids` and
    # `crm_notes.contact_ids` carry no type and are validated against nothing,
    # so a stale id here is invisible: the task renders, the contact chip is
    # blank, and no error is ever raised. Checked by asking whether any id in
    # them is a `crm_contacts` `_id`.
    crm_ids = set()
    async for row in db["crm_contacts"].find(dict(scope), {"_id": 1}):
        crm_ids.add(row["_id"])
    for collection in ("crm_tasks", "crm_notes"):
        q = dict(scope)
        q["contact_ids"] = {"$exists": True, "$ne": []}
        bad = 0
        async for row in db[collection].find(q, {"contact_ids": 1}):
            if any(cid in crm_ids for cid in (row.get("contact_ids") or [])):
                bad += 1
        if bad:
            failures.append(
                f"{bad} {collection} row(s) still hold a crm_contacts id in "
                f"`contact_ids`. Nothing validates that array, so this fails "
                f"SILENTLY after the drop — re-run the migration's step 4.")
        else:
            print(f"  {TICK} no {collection} row holds an old id")

    # ── 5. profiles whose person is gone ────────────────────────────────────
    missing = 0
    async for row in db["crm_contact_profiles"].find(dict(scope), {"contact_id": 1}):
        if await db["contacts"].count_documents(
                {"_id": row.get("contact_id")}, limit=1) == 0:
            missing += 1
    if missing:
        failures.append(
            f"{missing} profile(s) point at a contact that does not exist. "
            f"The migration half-ran, or somebody deleted people from the "
            f"audience. Investigate before removing the fallback.")
    else:
        print(f"  {TICK} every profile resolves to a person")

    print()
    for note in notes:
        print(f"  {WARN} {note}\n")
    if failures:
        print(f"{CROSS} NOT READY — phase 9 would destroy something:\n")
        for f in failures:
            print(f"    - {f}")
        print("\n`crm_contacts` is the rollback. Do not remove it yet.\n")
        await close_db()
        return 1

    print("✓ Nothing still depends on `crm_contacts`.\n")
    print("  That is the DATA answer. The remaining question is whether the")
    print("  unified path has run long enough, under real use, to give up the")
    print("  rollback — which is a judgement, not a query.\n")
    await close_db()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", default=None, help="check one account only")
    args = ap.parse_args()
    return asyncio.run(run(account=args.account))


if __name__ == "__main__":
    raise SystemExit(main())
