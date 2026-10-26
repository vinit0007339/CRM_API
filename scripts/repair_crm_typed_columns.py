#!/usr/bin/env python3
"""Find — and optionally clear — values that a typed CRM column cannot hold.

    python3 scripts/repair_crm_typed_columns.py            # report only
    python3 scripts/repair_crm_typed_columns.py --apply    # clear them

⚠️ REPORT-ONLY BY DEFAULT. With ``--apply`` the ONLY write is ``$set`` of the
offending field to ``null`` on the documents listed in the report. Nothing is
deleted, no document is removed, and every other field is left alone.

WHY THIS EXISTS. A deal was stored with

    expected_close_date = "cylonsign.com"

and from then on ``GET /crm/deals?pipeline_id=…`` answered **500 for the whole
pipeline** — the list builds a `DealModel` per row, that document cannot be
validated, and one bad record took out everybody's board. The write that
created it returned 200: pydantic does not validate on assignment, so a value
that reaches a typed column is written verbatim and only fails when the record
is READ BACK. A record whose typed column cannot be read was never saved, it
was corrupted.

The hole that let it in is closed (`attribute_routing._coerce` now refuses
instead of passing the value through, and the import worker routes through
that same call), but a fix to the code does not repair rows already on disk.
This script is the other half.

WHY RAW MOTOR, NOT BEANIE. Reading these documents through the models is
exactly what fails — Beanie would raise the same ValidationError this script
exists to find. So it reads dicts straight off the driver and does the type
check itself.

WHY NULL RATHER THAN A GUESS. "cylonsign.com" in a close-date column carries no
recoverable date; the column was mapped to the wrong file column. Nulling makes
the record readable again and leaves the field visibly empty, which is a state
the user can see and fix. Guessing a date would be invisible and wrong. The
discarded value is PRINTED for every document, so a repair can be reviewed
before it is made, and reversed by hand after.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from numbers import Number
from typing import Any, Dict, List, Tuple

sys.path.insert(0, ".")

from app.db import Database  # noqa: E402


def _is_number(value: Any) -> bool:
    # ⚠️ bool IS an int in Python, and True in an `employee_count` column is
    # not a number anybody meant. Checked before the Number test, which
    # accepts it.
    return not isinstance(value, bool) and isinstance(value, Number)


def _is_datetime(value: Any) -> bool:
    return isinstance(value, (datetime, date))


# ``{collection: {field: predicate}}`` — the typed columns on the CRM models,
# and what a readable value for each one looks like. Kept beside
# ``attribute_routing._NUMERIC_SYSTEM_FIELDS`` / ``_DATE_SYSTEM_FIELDS`` in
# spirit: that module decides what may be WRITTEN, this one what can be READ.
#
# ⚠️ THE COLLECTION NAMES COME FROM THE MODELS' `Settings.name`, NOT FROM A
# `crm_` PREFIX. Deals and companies predate the CRM prefix and live in
# `deals` and `companies`; only the CRM-native collections (`crm_notes`,
# `crm_tasks`, …) carry it. This script first shipped with `crm_deals` and
# `crm_companies`, which exist in no database — so it reported "nothing to
# repair" on the very data it was written for. Asserted below.
TYPED_COLUMNS: Dict[str, Dict[str, Any]] = {
    "deals": {
        "amount": _is_number,
        "probability": _is_number,
        "expected_close_date": _is_datetime,
        "closed_at": _is_datetime,
        "stage_entered_at": _is_datetime,
    },
    "companies": {
        "employee_count": _is_number,
        "annual_revenue": _is_number,
    },
}


def _assert_collections_exist_in_the_models() -> None:
    """Fail loudly at startup rather than reporting a clean database.

    A typo in a collection name here is invisible: every query returns
    nothing, the report says "Nothing to repair", and the corrupt rows stay
    exactly where they are. So the names are checked against the models that
    own them before anything is scanned.
    """
    from app.models.company import CompanyModel
    from app.models.deal import DealModel

    owners = {"deals": DealModel, "companies": CompanyModel}
    for collection in TYPED_COLUMNS:
        model = owners.get(collection)
        if model is None:
            raise SystemExit(
                f"{collection!r} has no model listed in this script — add it "
                f"to `owners` so its name can be verified.")
        actual = model.Settings.name
        if actual != collection:
            raise SystemExit(
                f"{model.__name__} lives in {actual!r}, not {collection!r}. "
                f"Fix TYPED_COLUMNS — a wrong name makes this script report a "
                f"clean database.")


async def scan(db, collection: str, columns: Dict[str, Any]
               ) -> List[Tuple[Any, str, Any]]:
    """``[(document_id, field, offending_value)]`` for one collection."""
    found: List[Tuple[Any, str, Any]] = []
    projection = {field: 1 for field in columns}
    projection["_id"] = 1
    projection["created_by"] = 1
    async for row in db[collection].find({}, projection):
        for field, ok in columns.items():
            value = row.get(field)
            if value is None:
                continue
            if not ok(value):
                found.append((row["_id"], field, value))
    return found


async def main(apply: bool) -> int:
    _assert_collections_exist_in_the_models()
    await Database.connect()
    db = Database.db

    total = 0
    for collection, columns in TYPED_COLUMNS.items():
        rows = await scan(db, collection, columns)
        total += len(rows)
        print(f"{collection}: {len(rows):,} unreadable value(s)")
        for doc_id, field, value in rows:
            # Printed one per line, with the value, because this is the only
            # record of what was there once --apply has run.
            print(f"    {doc_id}  {field} = {value!r}  ({type(value).__name__})")
        if apply and rows:
            for doc_id, field, _ in rows:
                await db[collection].update_one({"_id": doc_id},
                                                {"$set": {field: None}})
            print(f"    → cleared {len(rows):,} value(s)")
        print()

    if total == 0:
        print("Nothing to repair.")
    elif apply:
        print(f"APPLIED. {total:,} value(s) cleared.")
    else:
        print("REPORT ONLY. Re-run with --apply to clear the values above.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="Null the offending values (default: report only)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
