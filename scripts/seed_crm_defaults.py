"""Seed CRM defaults for one account, or for every account. CRM-005.

Idempotent and re-runnable — the house pattern (see ``seed_built_in_flows.py``).
Running it twice makes no changes the second time, and it never overwrites a
pipeline, setting or attribute an account has customised.

    poetry run python scripts/seed_crm_defaults.py --account 60f7c2b3c2a5f3e9a4d2a1c0
    poetry run python scripts/seed_crm_defaults.py --all

CRM-P0-002 backfill — always dry-run first and read the breakdown::

    poetry run python scripts/seed_crm_defaults.py \
        --crm-only --attributes-only --dry-run --verbose
    poetry run python scripts/seed_crm_defaults.py --crm-only --attributes-only

``--crm-only`` restricts the run to accounts that have actually used the CRM;
``--all`` means every user in the system, CRM user or not.

Note that ``GET /api/v1/crm/pipelines`` already provisions a default pipeline
on first use, so this script exists for backfilling existing accounts and for
seeding settings + system attributes, which the route does not create.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional, Set

# ⚠️ THE DB LIFECYCLE IS THE SERVICE'S NOW. In the monolith these
# scripts called `app.init_app_db` / `close_app_db`, which also
# registered every platform model. Here it is `app.db.init_db`, which
# registers the 14 CRM documents and nothing else — a script that
# cannot touch the platform's collections cannot migrate them by
# accident.
from app.config import logger as core_app_logger
from app.db import close_db, init_db
from app.models.attribute import CrmAttributeModel
from app.models.pipeline import PipelineModel, PipelineStage
from app.models.settings import CrmSettingsModel, LostReason
from app.object_id import PyObjectId

from app.services import defaults, stage_rules


class Report:
    """What a run did, so --dry-run and a real run print the same summary."""

    def __init__(self) -> None:
        self.pipelines = 0
        self.settings = 0
        self.attributes = 0
        # CRM-P0-002: a bare total cannot tell "18 contact fields missing on
        # every account" from "one field missing here and there", and those are
        # very different situations to walk into. Break it down.
        self.by_object: Dict[str, int] = {}
        self.accounts_touched: Set[str] = set()
        self.skipped: List[str] = []

    def attribute(self, account_id: str, object_type: str) -> None:
        self.attributes += 1
        self.by_object[object_type] = self.by_object.get(object_type, 0) + 1
        self.accounts_touched.add(account_id)

    def line(self) -> str:
        return (f"pipelines={self.pipelines} settings={self.settings} "
                f"attributes={self.attributes} skipped={len(self.skipped)}")

    def breakdown(self) -> str:
        if not self.attributes:
            return "  attributes: nothing missing"
        per = " ".join(
            f"{obj}={self.by_object[obj]}" for obj in sorted(self.by_object)
        )
        return (f"  attributes: {per}  "
                f"(across {len(self.accounts_touched)} account(s))")


async def seed_account(
    account_id: PyObjectId, *, dry_run: bool, report: Report,
    attributes_only: bool = False,
) -> None:
    """Seed one account. Every step checks before it writes.

    ``attributes_only`` is the CRM-213 backfill mode: accounts onboarded during
    Phase 1 have no ``crm_attributes`` rows, so their create forms fall back to
    a client-side field set instead of rendering from their own configuration.
    This mode adds the missing system attributes and touches nothing else — the
    pipelines and settings those accounts have customised are left alone, which
    makes it safe to run across a live account list.
    """
    label = str(account_id)

    if attributes_only:
        await _seed_system_attributes(account_id, dry_run=dry_run, report=report)
        return

    # ── pipeline ─────────────────────────────────────────────────────────────
    existing = await PipelineModel.find({"created_by": account_id}).to_list()
    if existing:
        report.skipped.append(f"{label}: has {len(existing)} pipeline(s)")
    else:
        rows = stage_rules.normalise_stages(defaults.build_default_stages())
        stage_rules.validate_stages(rows)          # never seed an invalid pipeline
        if not dry_run:
            await PipelineModel(
                created_by=account_id,
                name=defaults.DEFAULT_PIPELINE_NAME,
                is_default=True,
                stages=[PipelineStage(**r) for r in rows],
            ).insert()
        report.pipelines += 1

    # ── settings ─────────────────────────────────────────────────────────────
    if await CrmSettingsModel.find_one({"created_by": account_id}):
        report.skipped.append(f"{label}: settings exist")
    else:
        payload = defaults.default_settings_payload()
        payload["lost_reasons"] = [LostReason(**r) for r in payload["lost_reasons"]]
        if not dry_run:
            await CrmSettingsModel(created_by=account_id, **payload).insert()
        report.settings += 1

    # ── system attributes ────────────────────────────────────────────────────
    await _seed_system_attributes(account_id, dry_run=dry_run, report=report)


async def _seed_system_attributes(
    account_id: PyObjectId, *, dry_run: bool, report: Report
) -> None:
    """Add any missing ``is_system`` attribute rows. Idempotent per row.

    Checks per ``internal_name`` rather than "does this account have any
    attributes", so an account that was seeded before a new core field was
    added gets that one field and keeps everything else — including any custom
    attributes, which this never touches.
    """
    # ⚠️ DERIVED, NEVER HARD-CODED. This loop used to read
    # ("company", "deal") while `contact` was added to the catalogue in E25,
    # so the backfill silently skipped contacts for an entire phase — which is
    # the whole reason CRM-P0-002 exists. A new object type must be picked up
    # here the moment it joins SYSTEM_ATTRIBUTES_BY_OBJECT.
    for object_type in defaults.SYSTEM_OBJECT_TYPES:
        for row in defaults.build_system_attributes(object_type):
            found = await CrmAttributeModel.find_one({
                "created_by": account_id,
                "object_type": object_type,
                "internal_name": row["internal_name"],
            })
            if found:
                continue
            if not dry_run:
                await CrmAttributeModel(created_by=account_id, **row).insert()
            report.attribute(str(account_id), object_type)


async def crm_account_ids() -> List[PyObjectId]:
    """Accounts that have actually used the CRM. CRM-P0-002.

    ``--all`` means every document in ``users``, because an account *is* a user
    id (CRM plan §4.2) — including every user who has never opened the CRM and
    never will. Seeding those is harmless but writes roughly
    ``system_attribute_count()`` rows each for nothing, and it makes the
    dry-run number meaningless: you cannot tell real drift from untouched
    accounts in a single total.

    "Has used the CRM" is the union of four collections, because any one of
    them on its own is too narrow — an account can have a self-provisioned
    pipeline and nothing else, or companies imported before settings were ever
    saved.
    """
    from app.db import Database

    found: Set[Any] = set()
    for collection in ("crm_pipelines", "crm_settings",
                       "crm_deals", "crm_companies", "crm_contacts"):
        try:
            ids = await Database.db[collection].distinct("created_by")
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            continue
        found.update(i for i in ids if i)
    return list(found)


async def account_ids(
    explicit: Optional[str], everyone: bool, crm_only: bool = False
) -> List[PyObjectId]:
    """An account is a user id — there is no Account collection (CRM plan §4.2)."""
    if explicit:
        return [PyObjectId(explicit)]
    if crm_only:
        return await crm_account_ids()
    if not everyone:
        return []
    from app.db import Database
    users = await Database.db["users"].find({}, {"_id": 1}).to_list(length=None)
    return [u["_id"] for u in users]


async def main(args: Any) -> int:
    await init_db()
    try:
        targets = await account_ids(args.account, args.all, args.crm_only)
        if not targets:
            print("Nothing to do: pass --account <id>, --crm-only or --all.")
            return 1

        print(f"targets: {len(targets)} account(s)")
        report = Report()
        for acct in targets:
            try:
                await seed_account(acct, dry_run=args.dry_run, report=report,
                                   attributes_only=args.attributes_only)
            except Exception as ex:  # pylint: disable=broad-except
                _logger.exception(ex)
                print(f"  !! {acct}: {ex}")

        prefix = "[dry-run] would create" if args.dry_run else "created"
        print(f"{prefix}: {report.line()}")
        print(report.breakdown())
        if args.verbose:
            for row in report.skipped:
                print(f"  skipped {row}")
        return 0
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed CRM defaults (idempotent).")
    parser.add_argument("--account", help="Seed a single account id")
    parser.add_argument("--all", action="store_true",
                        help="Seed every user account, CRM user or not")
    parser.add_argument("--crm-only", action="store_true",
                        help="Seed only accounts that already have CRM data "
                             "(a pipeline, settings, deals, companies or "
                             "contacts). Preferred over --all for a backfill")
    parser.add_argument("--attributes-only", action="store_true",
                        help="CRM-213 backfill: add missing system attributes "
                             "only, leaving pipelines and settings untouched")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing")
    parser.add_argument("--verbose", action="store_true", help="List skipped items")
    sys.exit(asyncio.run(main(parser.parse_args())))
