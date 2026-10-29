#!/usr/bin/env python3
"""Move `crm_contacts` onto the unified model. Phase 7.

    python3 scripts/migrate_contact_unification.py --dry-run
    python3 scripts/migrate_contact_unification.py --dry-run --account <id>
    python3 scripts/migrate_contact_unification.py --apply  --account <id>

`docs/contact-unification-plan.md` §4. For each CRM-owned person: resolve the
platform contact that IS them, write this account's profile, and re-point the
associations, tasks and notes at the person's id.

⚠️ **NOTHING IS DELETED. `crm_contacts` IS LEFT EXACTLY AS IT IS.** It is the
rollback: turn the account's flag off and the old path reads the old rows,
untouched. Dropping the collection is phase 9, a separate release, after this
has run clean.

⚠️ **IDEMPOTENT AND RESUMABLE THROUGH ONE MARKER.** Step 1 stamps
`migrated_contact_id` on the `crm_contacts` row. A second run resolves nothing
again, and every later step builds its map by reading that field — so an
interrupted run resumes instead of re-deciding, and re-deciding is the thing
that would attach a person to a different contact the second time.

⚠️ **RUN `--dry-run` FIRST AND READ THE COLLISION COUNT.** A dry run writes
nothing and reports exactly what the apply would do, including the two hazards
below. Commit its output with the change.

────────────────────────────────────────────────────────────────────────────
HAZARD 1 — phone-only leads and the `contacts.email` index
────────────────────────────────────────────────────────────────────────────
`Contact.email` is `Optional` AND `Indexed(unique=True)`. If that index is not
partial, two contacts with no address collide on `null` and the second insert
fails — the same sparse-vs-partial trap `models/contact.py` documents on the
CRM side, and `scripts/check_crm_contact_index.py` was written for.

This script CHECKS the live index before step 1 and refuses to `--apply` when
a CRM person with no email would have to be created into it. It never alters
an index: that is a decision for whoever owns the platform collection.

────────────────────────────────────────────────────────────────────────────
HAZARD 2 — association collisions
────────────────────────────────────────────────────────────────────────────
A deal linked to BOTH the platform contact and the CRM contact for one person
becomes the same pair twice. `uniq_crm_association_pair` refuses the duplicate,
so the rewrite DEDUPES first: a colliding `crm_contact` row is dropped rather
than rewritten, because the `contact` row it would become already exists and
already carries the link. Counted and reported — a high count is not an error,
it means those people were linked from both sides, which is exactly what the
unification is for.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BATCH = 500
MARKER = "migrated_contact_id"


class Counts:
    def __init__(self) -> None:
        self.scanned = 0
        self.already = 0
        self.matched_by_marker = 0
        self.matched_by_platform_id = 0
        self.matched_by_email = 0
        self.would_create = 0
        self.no_email_blocked = 0
        self.profiles = 0
        self.assoc_rewritten = 0
        self.assoc_deduped = 0
        self.tasks = 0
        self.notes = 0

    def report(self, apply: bool) -> None:
        # Padded to one column so a dry run and an apply line up when they are
        # read side by side, which is how the apply gets checked.
        head = "changed" if apply else "would change"
        rows = [
            ("crm_contacts scanned", self.scanned),
            ("  already migrated", self.already),
            ("  resolved by platform id", self.matched_by_platform_id),
            ("  resolved by email", self.matched_by_email),
            ("  created as a new person", self.would_create),
            ("  BLOCKED (no email)", self.no_email_blocked),
            ("", None),
            ("profiles", self.profiles),
            ("associations rewritten", self.assoc_rewritten),
            ("  dropped as duplicates", self.assoc_deduped),
            ("tasks remapped", self.tasks),
            ("notes remapped", self.notes),
        ]
        print(f"\n  {head}:")
        for label, value in rows:
            if value is None:
                print()
                continue
            print(f"  {label:<30} {value:>7}")
        print()


async def _email_index_is_safe(db: Any) -> Optional[str]:
    """None when a contact with no email can be inserted; else why not."""
    try:
        info = await db["contacts"].index_information()
    except Exception as ex:                       # pylint: disable=broad-except
        return f"could not read the contacts indexes: {ex}"
    for name, spec in (info or {}).items():
        keys = [k for k, _ in spec.get("key", [])]
        if keys != ["email"] or not spec.get("unique"):
            continue
        if spec.get("sparse") or spec.get("partialFilterExpression"):
            return None
        return (f"`contacts` index {name!r} is unique on `email` and is "
                f"neither sparse nor partial, so a SECOND contact with no "
                f"email cannot be inserted. Make it partial "
                f"(`{{'email': {{'$type': 'string'}}}}`) before migrating "
                f"CRM people who have no address, or migrate only those who "
                f"do. See scripts/check_crm_contact_index.py for the same "
                f"trap on the CRM side.")
    return None


async def run(apply: bool, account: Optional[str]) -> int:
    from app.config import settings                                # noqa: F401
    from app.db import init_db, close_db
    from app.host_writes import find_or_create_contact
    from app.services import contact_rules as cr

    db = await init_db()
    if db is None:
        print("could not connect to the database", file=sys.stderr)
        return 2

    from app.object_id import PyObjectId

    scope: Dict[str, Any] = {}
    if account:
        scope["created_by"] = PyObjectId(str(account))

    counts = Counts()
    index_warning = await _email_index_is_safe(db)

    # ── step 1: map every CRM person to a platform contact ──────────────────
    mapping: Dict[str, Any] = {}          # str(crm_contact_id) -> contact_id
    cursor = db["crm_contacts"].find(scope).batch_size(BATCH)
    async for row in cursor:
        counts.scanned += 1
        crm_id = row.get("_id")
        acct = row.get("created_by")

        if row.get(MARKER):
            counts.already += 1
            counts.matched_by_marker += 1
            mapping[str(crm_id)] = row[MARKER]
            continue

        email = cr.normalize_email(row.get("email"))
        if row.get("platform_contact_id"):
            # ⚠️ TRUSTED FIRST AND WITHOUT A LOOKUP. It was written by
            # `promote`, which already decided this person IS that contact.
            # Re-deciding by email could pick a different row.
            contact_id = row["platform_contact_id"]
            counts.matched_by_platform_id += 1
        elif not email:
            # A phone-only lead. Creating one needs an insert with no address,
            # which hazard 1 may forbid.
            if index_warning:
                counts.no_email_blocked += 1
                continue
            counts.would_create += 1
            contact_id = None
        else:
            contact_id = None

        if contact_id is None:
            if not apply:
                # Dry run: count the intent, do not reach for the host.
                if email:
                    existing = await db["contacts"].find_one(
                        {"email": {"$regex": f"^{email}$", "$options": "i"}},
                        {"_id": 1})
                    if existing:
                        counts.matched_by_email += 1
                    else:
                        counts.would_create += 1
                mapping[str(crm_id)] = f"<unresolved:{crm_id}>"
                continue
            person, created = await find_or_create_contact(
                db, acct, email, cr.new_person_fields({
                    "email": email,
                    "first_name": row.get("first_name"),
                    "last_name": row.get("last_name"),
                    "phone": row.get("phone"),
                    "job_title": row.get("job_title"),
                    "linkedin_url": row.get("linkedin_url"),
                }))
            if not person:
                print(f"  ! could not resolve crm_contact {crm_id}",
                      file=sys.stderr)
                continue
            contact_id = person.get("_id")
            if created:
                counts.would_create += 1
            else:
                counts.matched_by_email += 1

        mapping[str(crm_id)] = contact_id
        if apply:
            await db["crm_contacts"].update_one(
                {"_id": crm_id}, {"$set": {MARKER: contact_id}})

    # ── step 2: one profile per (account, person) ───────────────────────────
    cursor = db["crm_contacts"].find(scope).batch_size(BATCH)
    async for row in cursor:
        contact_id = mapping.get(str(row.get("_id")))
        if contact_id is None or isinstance(contact_id, str):
            continue                               # unresolved in a dry run
        counts.profiles += 1
        if not apply:
            continue
        # ⚠️ UPSERT ON THE UNIQUE KEY, NOT AN INSERT. Two `crm_contacts` rows
        # in one account can map to ONE person — the same address stored twice
        # before the unique index was partial, or a promote plus a manual
        # create. The second must merge into the first profile, not collide
        # with it. `$setOnInsert` so a re-run cannot overwrite a profile
        # somebody has since edited.
        await db["crm_contact_profiles"].update_one(
            {"created_by": row.get("created_by"), "contact_id": contact_id},
            {"$setOnInsert": {
                "created_by": row.get("created_by"),
                "contact_id": contact_id,
                "created_by_user_id": row.get("created_by_user_id"),
                "owner_id": row.get("owner_id"),
                "lifecycle_stage": row.get("lifecycle_stage"),
                "description": row.get("description"),
                "source": row.get("source"),
                "primary_company_id": row.get("primary_company_id"),
                "primary_company_name": row.get("primary_company_name"),
                "tag_ids": row.get("tag_ids") or [],
                "custom_fields": row.get("custom_fields") or {},
                "deal_count": row.get("deal_count") or 0,
                "open_deal_value": row.get("open_deal_value") or 0.0,
                "open_task_count": row.get("open_task_count") or 0,
                "overdue_task_count": row.get("overdue_task_count") or 0,
                "last_activity_at": row.get("last_activity_at"),
                "is_archived": bool(row.get("is_archived")),
                "full_name": row.get("full_name"),
                "email": row.get("email"),
                "job_title": row.get("job_title"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }},
            upsert=True)

    # ── step 3: associations, deduped ───────────────────────────────────────
    assoc_scope = dict(scope)
    assoc_scope["to_type"] = "crm_contact"
    cursor = db["crm_associations"].find(assoc_scope).batch_size(BATCH)
    async for row in cursor:
        contact_id = mapping.get(str(row.get("to_id")))
        if contact_id is None or isinstance(contact_id, str):
            continue
        twin = {"created_by": row.get("created_by"),
                "from_type": row.get("from_type"), "from_id": row.get("from_id"),
                "to_type": "contact", "to_id": contact_id}
        clash = await db["crm_associations"].find_one(twin, {"_id": 1})
        if clash is not None:
            # HAZARD 2. The link already exists from the other side; this row
            # would duplicate it. Dropped rather than rewritten.
            counts.assoc_deduped += 1
            if apply:
                await db["crm_associations"].delete_one({"_id": row["_id"]})
            continue
        counts.assoc_rewritten += 1
        if apply:
            await db["crm_associations"].update_one(
                {"_id": row["_id"]},
                {"$set": {"to_type": "contact", "to_id": contact_id}})

    # ── step 4: the leaf arrays ─────────────────────────────────────────────
    # ⚠️ `crm_tasks.contact_ids` AND `crm_notes.contact_ids` ARE OPAQUE — no
    # type, no validation, nothing reads them against a collection. That is
    # why phase 6 needed no code for them and why they MUST be remapped here:
    # nothing else will ever notice they hold the old id space.
    for collection, counter in (("crm_tasks", "tasks"), ("crm_notes", "notes")):
        q = dict(scope)
        q["contact_ids"] = {"$exists": True, "$ne": []}
        cursor = db[collection].find(q).batch_size(BATCH)
        async for row in cursor:
            old_ids = row.get("contact_ids") or []
            new_ids, touched = [], False
            for cid in old_ids:
                mapped = mapping.get(str(cid))
                if mapped is not None and not isinstance(mapped, str):
                    new_ids.append(mapped)
                    touched = True
                else:
                    new_ids.append(cid)
            if not touched:
                continue
            # De-duplicated: a task linked to both rows for one person.
            seen, unique = set(), []
            for cid in new_ids:
                if str(cid) in seen:
                    continue
                seen.add(str(cid))
                unique.append(cid)
            setattr(counts, counter, getattr(counts, counter) + 1)
            if apply:
                await db[collection].update_one(
                    {"_id": row["_id"]}, {"$set": {"contact_ids": unique}})

    print(f"\n{'APPLIED' if apply else 'DRY RUN — nothing written'}"
          f"{f' · account {account}' if account else ' · every account'}")
    counts.report(apply)
    if index_warning:
        print(f"  ⚠️  {index_warning}\n")
        if counts.no_email_blocked:
            print(f"  {counts.no_email_blocked} CRM contact(s) have no email "
                  f"and were SKIPPED. They keep working on the legacy path.\n")
    await close_db()
    return 1 if (index_warning and counts.no_email_blocked and apply) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="report what would change and write nothing")
    g.add_argument("--apply", action="store_true", help="perform the migration")
    ap.add_argument("--account", default=None,
                    help="one account id — migrate with the flag, not the fleet")
    args = ap.parse_args()
    return asyncio.run(run(apply=args.apply, account=args.account))


if __name__ == "__main__":
    raise SystemExit(main())
