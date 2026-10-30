#!/usr/bin/env python3
"""Which currencies are actually in use, and where they disagree. CRM-440.

    python3 scripts/check_currency_usage.py
    python3 scripts/check_currency_usage.py --account <id>

⚠️ **IT READS AND REPORTS. IT WRITES NOTHING, EVER.** The backfill that fixes
what this finds is `scripts/backfill_deal_currency.py`, deliberately a separate
command: a check that can repair what it is checking is a check nobody trusts,
and money settings are the last place to blur that line.

Three questions:

  1. Which currencies do pipelines hold?   retired codes (EUR, GBP, AED, …)
                                           from when the picker offered seven
  2. Which hold an UNKNOWN code?           free text the old API accepted
  3. Do any leads disagree with their      a lead moved between pipelines
     pipeline?                             keeps its old currency, so one
                                           pipeline's totals silently mix

Exit code is 0 when nothing needs attention, 1 when something does, 2 when the
database could not be reached. A retired code is reported, never judged: an
account on EUR keeps it until a person decides otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TICK, CROSS, WARN = "✓", "✗", "!"


async def run(account: Optional[str]) -> int:
    from app.db import init_db, close_db
    from app.services import defaults

    db = await init_db()
    if db is None:
        print("could not connect to the database", file=sys.stderr)
        return 2

    from app.object_id import PyObjectId

    scope: Dict[str, Any] = {}
    if account:
        scope["created_by"] = PyObjectId(str(account))

    supported = set(defaults.SUPPORTED_CURRENCIES)
    problems: List[str] = []

    try:
        # ── 1 & 2. what pipelines hold ──────────────────────────────────────
        pipelines = await db["pipelines"].find(
            scope, {"_id": 1, "name": 1, "currency": 1, "created_by": 1},
        ).to_list(length=None)

        counts = Counter((p.get("currency") or "(unset)") for p in pipelines)
        print(f"\n  pipelines: {len(pipelines)}")
        for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            mark = TICK if code in supported or code == "(unset)" else WARN
            note = ""
            if code == "(unset)":
                note = "  — falls back to the account default"
            elif code not in supported:
                note = "  — RETIRED, kept as-is; change it on the pipeline to move off"
            print(f"    {mark} {code:10} {n}{note}")

        retired = [p for p in pipelines
                   if p.get("currency") and p["currency"] not in supported]
        if retired:
            problems.append(
                f"{len(retired)} pipeline(s) on a currency the picker no longer "
                f"offers. They keep working; nothing is rewritten for them.")
            for p in retired[:10]:
                print(f"      · {p.get('name')!r} ({p['currency']}) "
                      f"account={p.get('created_by')}")

        # ── 3. leads that disagree with their pipeline ──────────────────────
        #
        # ⚠️ THE JOIN IS DONE HERE, NOT IN MONGO. `pipelines` is small — one
        # account has a handful — so a dict beats a $lookup, and this script
        # has to run against a shared database where an aggregation on `deals`
        # is the expensive thing in the room.
        by_id = {p["_id"]: (p.get("currency") or None) for p in pipelines}
        mismatched: List[Dict[str, Any]] = []
        cursor = db["deals"].find(
            scope, {"_id": 1, "name": 1, "currency": 1, "pipeline_id": 1},
        )
        total_deals = 0
        async for deal in cursor:
            total_deals += 1
            want = by_id.get(deal.get("pipeline_id"))
            have = deal.get("currency")
            # An unset pipeline currency makes no claim, so a lead under it
            # cannot disagree with anything.
            if want and have and have != want:
                mismatched.append(deal)

        print(f"\n  leads: {total_deals}")
        if mismatched:
            print(f"    {CROSS} {len(mismatched)} disagree with their pipeline")
            for d in mismatched[:10]:
                print(f"      · {d.get('name')!r}: lead={d.get('currency')} "
                      f"pipeline={by_id.get(d.get('pipeline_id'))}")
            if len(mismatched) > 10:
                print(f"      … and {len(mismatched) - 10} more")
            problems.append(
                f"{len(mismatched)} lead(s) hold a different currency from their "
                f"pipeline, so that pipeline's totals add up mixed money. "
                f"Fix with: python3 scripts/backfill_deal_currency.py --apply")
        else:
            print(f"    {TICK} every lead agrees with its pipeline")
    finally:
        await close_db()

    print()
    if problems:
        for p in problems:
            print(f"  {WARN} {p}")
        print("\nFAIL — see above")
        return 1
    print("PASS — currencies are consistent")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", help="limit to one account id")
    args = ap.parse_args()
    return asyncio.run(run(args.account))


if __name__ == "__main__":
    raise SystemExit(main())
