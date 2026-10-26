"""Board & stage-move latency benchmark. CRM-061.

    poetry run python scripts/benchmark_crm_board.py --account <ACCOUNT_ID>
    poetry run python scripts/benchmark_crm_board.py --account <ID> --cleanup

Seeds 1,000 deals across 10 stages, then measures:

    NFR1  GET /crm/pipelines/{id}/board                p95 < 500 ms
    NFR2  PATCH /crm/deals/{id}/stage                  p95 < 400 ms

**Record the numbers in the PR.** If a target is missed, raise a follow-up
ticket rather than quietly accepting it — the whole point of a number in an AC
is that someone has to look at it.

WHAT THIS MEASURES, AND WHAT IT DOES NOT

It calls the CRUD and service layer directly against a real MongoDB, so it
measures the query plans and the aggregation — the parts that actually scale.
It does NOT measure FastAPI serialisation, network, or auth. Those are roughly
constant and small next to a board query over 1,000 documents; if you need the
end-to-end figure, put a load generator in front of the running service.

It writes into the account you name, under a benchmark-only pipeline, and
`--cleanup` removes exactly what it created. Do not point it at production
without `--cleanup`, and preferably not at all.

WHY THE BOARD IS THE THING TO WATCH. `board_summary` issues one aggregation for
the totals plus one indexed read per stage — N+1 by shape, where N is the stage
count. That is deliberate (see the docstring in `app/db/crud/main/crm/deal.py`)
and fine at 10 stages, but it is exactly the kind of decision that stops being
fine quietly. This script is how you find out.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from typing import List


from app.config import logger as _logger
from app.db import Database, close_db, init_db
from app.object_id import PyObjectId
from app.models.deal import DealModel
from app.models.pipeline import PipelineModel, PipelineStage
from app.crud import DealCrud, PipelineCrud
from app.services import position as pos, stage_rules

DEALS = 1000
STAGES = 10
BOARD_RUNS = 40
MOVE_RUNS = 40

NFR1_MS = 500.0   # board p95
NFR2_MS = 400.0   # stage move p95

BENCH_PIPELINE_NAME = "__benchmark__ CRM-061"


def p95(samples: List[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    # Nearest-rank, which is what "p95" means for a sample this size. Do not
    # interpolate: with 40 runs an interpolated p95 hides the slowest call,
    # and the slowest call is the whole question.
    index = max(0, min(len(ordered) - 1, int(round(0.95 * len(ordered))) - 1))
    return ordered[index]


def report(name: str, samples: List[float], budget: float) -> bool:
    hit = p95(samples)
    ok = hit < budget
    print(
        f"  {name:<26} n={len(samples):>4}  "
        f"min {min(samples):7.1f}  med {statistics.median(samples):7.1f}  "
        f"p95 {hit:7.1f}  max {max(samples):7.1f}  ms   "
        f"[budget {budget:.0f} ms] {'PASS' if ok else 'FAIL'}"
    )
    return ok


async def connect():
    """The service's own connection, not a hand-rolled one.

    ⚠️ IT USED TO PARSE THE DATABASE NAME OUT OF THE URI
    (`uri.rsplit("/", 1)[-1].split("?")[0]`), which is wrong for every
    `mongodb+srv://host/?retryWrites=true` form — there is no database in the
    path, so it benchmarked a database called "" or "retryWrites=true". Going
    through `app.db` means the benchmark reads the same config, the same
    client options (no `tz_aware`, the certifi fallback) and the same 14
    models the service itself runs with. A benchmark against a
    differently-configured client measures the wrong thing.
    """
    await init_db()
    if Database.db is None:
        raise SystemExit("no database configured — see config/app.conf")
    return Database.db


def build_stages() -> List[PipelineStage]:
    rows = []
    for i in range(STAGES - 2):
        rows.append(PipelineStage(
            id=PyObjectId(), name=f"Bench {i + 1}", order=i, type="open",
            win_probability=int(100 * (i + 1) / (STAGES - 1)), rotting_days=14,
        ))
    rows.append(PipelineStage(id=PyObjectId(), name="Bench Won",
                              order=STAGES - 2, type="won", win_probability=100))
    rows.append(PipelineStage(id=PyObjectId(), name="Bench Lost",
                              order=STAGES - 1, type="lost", win_probability=0))
    return rows


async def seed(account_id: PyObjectId) -> PipelineModel:
    existing = await PipelineModel.find_one(
        {"created_by": account_id, "name": BENCH_PIPELINE_NAME})
    if existing:
        print(f"  reusing existing benchmark pipeline {existing.id}")
        return existing

    pipeline = await PipelineModel(
        created_by=account_id, name=BENCH_PIPELINE_NAME,
        description="Created by scripts/benchmark_crm_board.py — safe to delete.",
        is_default=False, weighted_forecast_enabled=True, currency="INR",
        stages=build_stages(),
    ).insert()

    open_stages = [s for s in pipeline.stages if s.type == "open"]
    rng = random.Random(20260813)          # fixed seed: comparable runs
    batch = []
    for i in range(DEALS):
        stage = open_stages[i % len(open_stages)]
        batch.append(DealModel(
            created_by=account_id, name=f"Bench deal {i}",
            pipeline_id=pipeline.id, stage_id=stage.id,
            amount=rng.choice([None, 50000.0, 250000.0, 1000000.0]),
            # A third carry an override, so the weighted aggregation does real
            # work rather than the trivial all-stage-default case.
            probability=rng.choice([None, None, 10, 60, 90]),
            position=pos.STEP * (i + 1), status="open",
        ))
    await DealModel.insert_many(batch)
    print(f"  seeded {DEALS} deals across {len(open_stages)} open stages")
    return pipeline


async def cleanup(account_id: PyObjectId):
    pipeline = await PipelineModel.find_one(
        {"created_by": account_id, "name": BENCH_PIPELINE_NAME})
    if not pipeline:
        print("  nothing to clean up")
        return
    deleted = await DealModel.find(
        {"created_by": account_id, "pipeline_id": pipeline.id}).delete()
    await pipeline.delete()
    print(f"  removed the benchmark pipeline and {getattr(deleted, 'deleted_count', '?')} deals")


async def bench_board(db, account_id, pipeline) -> List[float]:
    crud = DealCrud(db_session=db)
    samples = []
    for _ in range(BOARD_RUNS):
        start = time.perf_counter()
        await crud.board_summary(account_id=account_id, pipeline=pipeline,
                                 cards_per_stage=25)
        samples.append((time.perf_counter() - start) * 1000)
    return samples


async def bench_move(db, account_id, pipeline) -> List[float]:
    crud = DealCrud(db_session=db)
    open_stages = [s.model_dump() for s in pipeline.stages if s.type == "open"]
    deals = await DealModel.find(
        {"created_by": account_id, "pipeline_id": pipeline.id}
    ).limit(MOVE_RUNS).to_list()

    samples = []
    for i, deal in enumerate(deals):
        target = open_stages[(i + 1) % len(open_stages)]
        start = time.perf_counter()
        # The same work the handler does, minus HTTP: read the column, compute
        # the position, write the deal.
        column = await crud.column_positions(
            account_id=account_id, pipeline_id=pipeline.id, stage_id=target["id"])
        new_position = pos.append_position(column[-1] if column else None)
        deal.stage_id = target["id"]
        deal.status = stage_rules.status_for_stage(target)
        deal.position = new_position
        await deal.save()
        samples.append((time.perf_counter() - start) * 1000)
    return samples


async def explain(db, account_id, pipeline):
    """Attach this to the PR — CRM-008 and CRM-022 both ask for it."""
    stage = next(s for s in pipeline.stages if s.type == "open")
    plan = await db.command({
        "explain": {
            "find": "deals",
            "filter": {"created_by": account_id, "pipeline_id": pipeline.id,
                       "stage_id": stage.id},
            "sort": {"position": 1}, "limit": 25,
        },
        "verbosity": "queryPlanner",
    })
    winning = plan.get("queryPlanner", {}).get("winningPlan", {})
    text = str(winning)
    stage_name = "IXSCAN" if "IXSCAN" in text else "COLLSCAN"
    print(f"  column query plan: {stage_name}")
    if stage_name != "IXSCAN":
        print("  !! the board index is not being used — check "
              "(created_by, pipeline_id, stage_id, position)")
    return winning


async def main():
    parser = argparse.ArgumentParser(description="CRM board benchmark (CRM-061)")
    parser.add_argument("--account", required=True, help="account id (the owner user's _id)")
    parser.add_argument("--cleanup", action="store_true", help="remove benchmark data and exit")
    args = parser.parse_args()

    account_id = PyObjectId(args.account)
    db = await connect()

    if args.cleanup:
        print("Cleanup:")
        await cleanup(account_id)
        return 0

    print(f"CRM-061 benchmark — account {account_id}")
    print("Seeding:")
    pipeline = await seed(account_id)
    crud = PipelineCrud(db_session=db)
    pipeline = await crud.get_scoped(account_id, pipeline.id)

    print("Query plan:")
    await explain(db, account_id, pipeline)

    print("Results:")
    board_ok = report("board (NFR1)", await bench_board(db, account_id, pipeline), NFR1_MS)
    move_ok = report("stage move (NFR2)", await bench_move(db, account_id, pipeline), NFR2_MS)

    print()
    if board_ok and move_ok:
        print("PASS — both budgets met. Paste this output into the PR.")
    else:
        print("FAIL — a budget was missed. Raise a follow-up ticket with this "
              "output rather than adjusting the budget.")
    print(f"\nRun with --cleanup to remove the '{BENCH_PIPELINE_NAME}' pipeline "
          f"and its {DEALS} deals.")
    return 0 if (board_ok and move_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
