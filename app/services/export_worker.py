"""Builds an export file. CRM-236.

STREAMS, DOES NOT BUFFER. Records are read a page at a time and written
straight into a spooled temporary file, so peak memory is one page plus the
CSV writer's buffer rather than the whole account. A 50,000-record export with
40 columns is a few hundred megabytes as one Python string, and three of those
at once is the process.

THE EXPORT USES THE SAME FILTER BUILDER AS THE LIST ENDPOINT. Not a similar
one — the same ``crud.list`` call with the same keyword filters. An export that
resolved its filters independently would eventually disagree with the screen it
was launched from, and the direction that disagreement goes is "the file
contains records the caller could not see".

Read in this module is account-wide, matching the list endpoints: ownership
narrows writes, never reads (see ``list_companies``). If that ever changes, the
list and the export must change together — ``test_e17_exports`` asserts they
return the same set.
"""
from __future__ import annotations

import io
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.config import logger as _logger
from app.models.export_run import EXPORT_TTL_DAYS
from app.object_id import PyObjectId

from app.services import export_rules as er, file_rules, file_storage

# Records fetched per query. Big enough that a 50k export is 50 queries, small
# enough that one page is a few megabytes.
PAGE_SIZE = 1000

# Above this the file is spooled to disk instead of being held in memory.
SPOOL_BYTES = 8 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExportRunner:
    """Owns one export job. Instantiated by the route, run in the background."""

    def __init__(self, *, export_id: Any, account_id: Any, entity_type: str,
                 filters: Dict[str, Any], columns: List[str], separator: str,
                 tz_name: Optional[str] = None, records_crud=None,
                 exports=None, attributes=None):
        self.export_id = export_id
        self.account_id = PyObjectId(str(account_id))
        self.entity_type = entity_type
        self.filters = dict(filters or {})
        self.columns = list(columns or [])
        self.separator = separator
        self.tz_name = tz_name
        self.records = records_crud
        self.exports = exports
        self.attributes = attributes

    async def run(self) -> Dict[str, Any]:
        await self._patch(status="running", started_at=_now())
        try:
            definitions = await self.attributes.list(
                account_id=self.account_id, object_type=self.entity_type)
            columns = er.resolve_columns(self.columns, definitions,
                                         self.entity_type)

            written, size, key, name = await self._build(columns)
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
            await self._patch(status="failed", finished_at=_now(),
                              message="The export could not be produced.")
            return {"status": "failed"}

        if key is None:
            await self._patch(status="failed", finished_at=_now(),
                              message="The export file could not be stored.")
            return {"status": "failed"}

        await self._patch(
            status="completed", finished_at=_now(), row_count=written,
            total_rows=written, storage_key=key, file_name=name, file_size=size,
            # Set here, not at creation: the clock that matters is when the
            # file came into existence.
            expires_at=_now() + timedelta(days=EXPORT_TTL_DAYS),
            message=f"{written:,} rows exported.")
        return {"status": "completed", "rows": written}

    # ── the file ─────────────────────────────────────────────────────────────
    async def _build(self, columns):
        """Page through the records, writing as we go."""
        written = 0
        # SpooledTemporaryFile keeps small exports in memory and rolls the big
        # ones onto disk on its own. Most exports are small; the ones that are
        # not are exactly the ones that must not be held in RAM.
        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES, mode="w+",
                                              encoding="utf-8", newline="")
        try:
            # The BOM is what makes Excel read the file as UTF-8. Without it,
            # every accented and Devanagari name in the export is mojibake —
            # and the user's first assumption is that we stored it wrong.
            spool.write(er.BOM)
            writer = er.make_writer(spool, self.separator)
            writer.writerow(er.header_row(columns))

            skip = 0
            while True:
                page = await self._page(skip)
                if not page:
                    break
                written += er.write_rows(writer, columns, page, self.tz_name)
                if len(page) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE
                await self._patch(row_count=written)

            spool.seek(0)
            body = spool.read().encode("utf-8")
        finally:
            spool.close()

        name = er.filename(self.entity_type, _now(),
                           filtered=bool(er.describe_filters(self.filters)
                                         != "All records"))
        # Third arg is the EXTENSION, not the filename — the key is random by
        # design so a user-supplied name can never influence where the object
        # lands. Passing `name` here would put "companies-2026-08-15.csv" into
        # the key and quietly defeat that.
        key = file_rules.storage_key(self.account_id, "export", ".csv")
        ok, _error = file_storage.put(key, body, "text/csv; charset=utf-8")
        if not ok:
            return written, 0, None, name
        return written, len(body), key, name

    async def _page(self, skip: int) -> List[Any]:
        """One page, through the SAME call the list endpoint makes.

        ``record_ids`` is handled here rather than as a filter because it is
        not one: it is an explicit selection from the screen, and it must still
        be account-scoped — ids arriving from a client are not proof of
        ownership.
        """
        ids = self.filters.get("record_ids")
        if ids:
            page = ids[skip:skip + PAGE_SIZE]
            out = []
            for record_id in page:
                found = await self.records.get_scoped(self.account_id, record_id)
                if found is not None:
                    out.append(found)
            return out

        filters = {k: v for k, v in self.filters.items() if k != "record_ids"}
        return await self.records.list(
            account_id=self.account_id, skip=skip, limit=PAGE_SIZE, **filters)

    async def _patch(self, **fields: Any) -> None:
        fields["updated_at"] = _now()
        try:
            await self.exports.update_scoped(self.account_id, self.export_id,
                                             fields)
        except Exception as ex:  # pylint: disable=broad-except
            # Progress is not the export. Losing a progress write must not lose
            # the file.
            _logger.exception(ex)


async def sweep_expired(exports, *, now: Optional[datetime] = None,
                        limit: int = 500) -> int:
    """Delete export files past their retention date. Returns how many.

    An export nobody deletes is a copy of the customer list that outlives the
    reason it was made. The JOB ROW is kept — the history of who exported what
    is worth more than the bytes, and is much smaller — but the object goes and
    ``storage_key`` is cleared so a later poll says "expired" rather than
    minting a URL for an object that is not there.
    """
    now = now or _now()
    removed = 0
    try:
        rows = await exports.model.find({
            "expires_at": {"$type": "date", "$lte": now},
            "storage_key": {"$ne": None},
        }).limit(limit).to_list()
    except Exception as ex:  # pylint: disable=broad-except
        _logger.exception(ex)
        return 0

    for row in rows:
        try:
            file_storage.delete(row.storage_key)
        except Exception as ex:  # pylint: disable=broad-except
            # Better an orphaned object than a row that keeps claiming a file
            # exists. The key is cleared either way.
            _logger.exception(ex)
        try:
            row.storage_key = None
            row.message = "This export has expired. Run it again for a fresh copy."
            row.updated_at = now
            await row.save()
            removed += 1
        except Exception as ex:  # pylint: disable=broad-except
            _logger.exception(ex)
    return removed
