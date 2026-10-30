#!/usr/bin/env python3
"""Put every lead on its pipeline's currency. CRM-440, phase 1.

    python3 scripts/backfill_deal_currency.py                 # dry run
    python3 scripts/backfill_deal_currency.py --apply
    python3 scripts/backfill_deal_currency.py --apply --account <id>

WHY THIS IS NEEDED. Until phase 1 a lead's currency came from the request
(`payload.currency or pipeline.currency`) and a move between pipelines left it
alone — so a pipeline could hold leads in two currencies and its column totals
added them up as bare numbers. The code no longer creates that state;
this repairs the rows that already have it.

⚠️ **IT CHANGES THE CURRENCY, NEVER THE AMOUNT.** 5,000 stays 5,000. This is
re-labelling a row that was mis-labelled, not converting money — there are no
rates here and inventing one would turn a visible inconsistency into an
invisible wrong number. A lead whose amount genuinely needs restating is a
judgement for the person who owns it, and `--report` lists them so that
judgement can be made.

⚠️ **DRY RUN BY DEFAULT.** Nothing is written without `--apply`.

⚠️ **A PIPELINE WITH NO CURRENCY MAKES NO CLAIM**, so leads under it are left
exactly as they are: there is nothing to agree with, and blanking a lead's
currency would lose the only information present.
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

TICK, WARN = "✓", "!"


async def run(account: Optional[str], apply: bool, report: bool) -> int:
    from app.db import init_db, close_db

    db = await init_db()
    if db is None:
        print("could not connect to the database", file=sys.stderr)
        return 2

    from app.object_id import PyObjectId

    scope: Dict[str, Any] = {}
    if account:
        scope["created_by"] = PyObjectId(str(account))

    try:
        pipelines = await db["pipelines"].find(
            scope, {"_id": 1, "name": 1, "currency": 1}).to_list(length=None)
        by_id = {p["_id"]: (p.get("currency") or None) for p in pipelines}

        moves: List[Dict[str, Any]] = []
        async for deal in db["deals"].find(
                scope, {"_id": 1, "name": 1, "amount": 1,
                        "currency": 1, "pipeline_id": 1}):
            want = by_id.get(deal.get("pipeline_id"))
            have = deal.get("currency")
            if want and have and have != want:
                moves.append({**deal, "want": want})

        if not moves:
            print(f"  {TICK} every lead already agrees with its pipeline")
            return 0

        pairs = Counter(f"{d['currency']} -> {d['want']}" for d in moves)
        print(f"\n  {len(moves)} lead(s) to re-label:")
        for pair, n in sorted(pairs.items(), key=lambda kv: -kv[1]):
            print(f"    {pair:16} {n}")

        # ⚠️ THE ONES WITH MONEY ON THEM ARE CALLED OUT SEPARATELY. Re-labelling
        # a lead with no amount cannot change any total; re-labelling one with
        # 5,000 on it moves 5,000 from one currency's subtotal to another's.
        # Those are the rows a person should actually look at.
        with_amount = [d for d in moves if d.get("amount")]
        if with_amount:
            print(f"\n  {WARN} {len(with_amount)} of them carry an amount — the "
                  f"figure is NOT converted, only re-labelled:")
            for d in with_amount[:20] if not report else with_amount:
                print(f"      · {d.get('name')!r}  {d.get('amount')} "
                      f"{d.get('currency')} -> {d['want']}")
            if not report and len(with_amount) > 20:
                print(f"      … and {len(with_amount) - 20} more (--report for all)")

        if not apply:
            print("\n  dry run — nothing written. Re-run with --apply.")
            return 1

        changed = 0
        for d in moves:
            res = await db["deals"].update_one(
                {"_id": d["_id"]}, {"$set": {"currency": d["want"]}})
            changed += int(getattr(res, "modified_count", 0) or 0)
        print(f"\n  {TICK} re-labelled {changed} lead(s)")
        return 0
    finally:
        await close_db()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", help="limit to one account id")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--report", action="store_true",
                    help="list every affected lead, not just the first 20")
    args = ap.parse_args()
    return asyncio.run(run(args.account, args.apply, args.report))


if __name__ == "__main__":
    raise SystemExit(main())
