"""Parsing, sniffing and mapping for CSV/XLSX imports. CRM-229 / CRM-230.

REAL CRM EXPORTS ARE NOT UTF-8 COMMA-SEPARATED. That assumption is the single
most common way an import feature fails in front of a customer:

* Excel's "CSV UTF-8" writes a **BOM**, so the first header arrives as
  ``\\ufeffname`` and matches nothing — the user sees every column unmapped and
  concludes the feature is broken.
* Excel's "Unicode text (*.txt)" is **UTF-16LE, tab-separated**.
* A European Excel writes **semicolons**, because the comma is the decimal
  separator there.
* Anything exported from an older system is often **cp1252**, and decoding it
  as UTF-8 either raises or mojibakes every accented name permanently.

So encoding and delimiter are *detected*, never assumed, and detection never
raises — an undecodable byte becomes a replacement character in one cell rather
than a 500 for the whole file.

ROW NUMBERS ARE THE NUMBERS THE USER SEES IN EXCEL. The error report is
worthless if the numbers in it cannot be looked up in the source file, so a row
number here counts the header as row 1 and counts a quoted newline as part of
its record, exactly as a spreadsheet does — not as physical lines in the file.

COLUMNS ARE ADDRESSED BY INDEX, NOT BY HEADER TEXT. Exports with two columns
both called "Name" are common, and keying a mapping by header string silently
drops one of them.

Stdlib + openpyxl. No DB, no pydantic, no FastAPI.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

# Kept well above the 10 MB attachment cap in ``file_rules``: an import file is
# not an attachment, and a 50k-row company export is legitimately large. It is
# still bounded, because the whole file is read into memory to sniff it.
MAX_IMPORT_BYTES = 50 * 1024 * 1024
MAX_ROWS = 100_000
PREVIEW_ROWS = 20

# How many rejected rows are kept for the error report. Past this the counters
# stay exact and a flag says the samples were capped — a document holding
# 100k errors would exceed Mongo's 16 MB limit, which is how the existing list
# import learned this (see ``ListImportModel.errors_truncated``).
MAX_ERROR_ROWS = 5_000

DELIMITERS = (",", ";", "\t", "|")

_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


# ── file kind ────────────────────────────────────────────────────────────────
def is_xlsx(raw: bytes) -> bool:
    """XLSX is a zip. Sniffed from the bytes, never from the extension.

    Users rename files, and a ``.csv`` that is really a workbook would
    otherwise be parsed as one enormous unreadable line.
    """
    return bool(raw[:4] == b"PK\x03\x04")


def is_legacy_xls(raw: bytes) -> bool:
    """The old OLE2 ``.xls``. Detected only so the message can say so.

    "Unsupported file" sends the user back to try the same file again;
    "save it as .xlsx or .csv" tells them what to do.
    """
    return bool(raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


# ── encoding ─────────────────────────────────────────────────────────────────
def sniff_encoding(raw: bytes) -> str:
    """Best guess at the text encoding. Never raises.

    BOM first — it is a *statement* of encoding and beats any heuristic. Then
    chardet if it is confident. Then UTF-8, which is right for most modern
    files and, importantly, *fails loudly* on cp1252 bytes rather than
    mojibaking them. Only then cp1252, which decodes anything.
    """
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return encoding

    try:
        import chardet

        guess = chardet.detect(raw[:200_000]) or {}
        name = (guess.get("encoding") or "").lower()
        # Below ~0.7 chardet is guessing from very little evidence, and its
        # wrong guesses (koi8-r for a mostly-ASCII file) are worse than utf-8.
        if name and (guess.get("confidence") or 0) >= 0.7:
            if name in ("ascii", "utf-8"):
                return "utf-8"
            return name
    except Exception:  # pylint: disable=broad-except
        pass

    try:
        raw[:200_000].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def decode(raw: bytes, encoding: Optional[str] = None) -> str:
    """Decode with replacement rather than raising.

    One bad byte should cost one garbled character in one cell, not the whole
    import. The alternative — refusing the file — leaves the user with no way
    to find out which byte it was.
    """
    encoding = encoding or sniff_encoding(raw)
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    # A UTF-8 BOM survives an explicit "utf-8" decode as ﻿. Left in place,
    # it becomes part of the first header and that column maps to nothing.
    return text.lstrip("﻿")


# ── delimiter ────────────────────────────────────────────────────────────────
def sniff_delimiter(text: str) -> str:
    """Which character separates the columns.

    ``csv.Sniffer`` first, then a count of candidates in the header line.
    Counting the *header* rather than the whole file matters: an address field
    full of commas would otherwise outvote the semicolons that actually
    separate the columns.
    """
    sample = text[:64_000]
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS)).delimiter
    except Exception:  # pylint: disable=broad-except
        pass

    header = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {d: header.count(d) for d in DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    # No separator anywhere means a single-column file, which is still valid.
    return best if counts[best] else ","


# ── reading ──────────────────────────────────────────────────────────────────
def read_rows(raw: bytes, *, limit: Optional[int] = None
              ) -> Tuple[List[str], List[List[str]], Dict[str, Any]]:
    """``(headers, rows, meta)``. One entry point for CSV, TSV and XLSX.

    ``meta`` carries what was detected, so the UI can show it and the user can
    tell at a glance whether the file was understood — "Detected: UTF-16,
    tab-separated" is the fastest way for someone to spot that they exported
    the wrong thing.
    """
    if is_legacy_xls(raw):
        raise ValueError(
            "That's an old-format .xls file. Open it in Excel and save it as "
            ".xlsx or .csv, then try again.")

    if is_xlsx(raw):
        headers, rows = _read_xlsx(raw, limit=limit)
        return headers, rows, {"format": "xlsx", "encoding": None, "delimiter": None}

    encoding = sniff_encoding(raw)
    text = decode(raw, encoding)
    delimiter = sniff_delimiter(text)
    headers, rows = _read_csv(text, delimiter, limit=limit)
    return headers, rows, {
        "format": "csv", "encoding": encoding, "delimiter": delimiter,
    }


def _read_csv(text: str, delimiter: str, *, limit: Optional[int] = None
              ) -> Tuple[List[str], List[List[str]]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        headers = next(reader)
    except StopIteration:
        return [], []
    headers = [str(h or "").strip().lstrip("﻿") for h in headers]

    rows: List[List[str]] = []
    for row in reader:
        if limit is not None and len(rows) >= limit:
            break
        rows.append([("" if cell is None else str(cell)) for cell in row])
    return headers, rows


def _read_xlsx(raw: bytes, *, limit: Optional[int] = None
               ) -> Tuple[List[str], List[List[str]]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise ValueError("Excel files can't be read on this server. "
                         "Save the sheet as CSV and try again.") from exc

    try:
        # read_only + data_only: we want the CACHED VALUE of a formula cell,
        # not "=VLOOKUP(...)". A user who built their sheet with formulas would
        # otherwise import the formula text into every record.
        book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True,
                                      data_only=True)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError("That file couldn't be opened as a spreadsheet.") from exc

    sheet = book[book.sheetnames[0]]
    headers: List[str] = []
    rows: List[List[str]] = []
    for index, row in enumerate(sheet.iter_rows(values_only=True)):
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        if index == 0:
            headers = [c.lstrip("﻿") for c in cells]
            continue
        if limit is not None and len(rows) >= limit:
            break
        rows.append(cells)
    try:
        book.close()
    except Exception:  # pylint: disable=broad-except
        pass
    return headers, rows


def is_blank_row(row: Sequence[Any]) -> bool:
    """A row of nothing.

    Spreadsheets routinely carry thousands of these after the last real record,
    because someone once clicked in a far-down cell. Reporting each as an error
    buries the three real problems under five thousand fake ones.
    """
    return all(str(cell or "").strip() == "" for cell in (row or []))


def file_row_number(row_index: int, *, has_header: bool = True) -> int:
    """The row number a spreadsheet would show for data row ``row_index``.

    Zero-based data index → one-based file row, plus one for the header. This
    exists as a named function with tests rather than as ``i + 2`` scattered
    through the worker, because every off-by-one here produces an error report
    that cannot be lined up against the source file — which the AC calls out
    precisely because it is the usual outcome.

    Note this counts RECORDS, not physical lines: a quoted newline inside a
    cell keeps its record on one row, which is what Excel shows too.
    """
    return row_index + (2 if has_header else 1)


# ── header → attribute mapping ───────────────────────────────────────────────
def normalise_header(value: Any) -> str:
    """Reduce a header to something comparable: lowercase, alphanumeric only."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


# Header spellings seen in the wild that do not resemble our internal names.
# Deliberately conservative: a wrong auto-mapping is worse than none, because
# the user reviews a screen of already-filled dropdowns and trusts them.
SYNONYMS: Dict[str, Tuple[str, ...]] = {
    # The id column an export produced. Mapping it is what turns an edited
    # export into an update rather than a second copy of everything.
    "company_id": ("id", "companyid", "recordid", "crmid"),
    "name": ("companyname", "company", "account", "accountname", "organisation",
             "organization", "orgname", "businessname", "dealname", "title",
             "opportunity", "opportunityname"),
    "domain": ("website", "url", "web", "site", "companydomain", "webaddress",
               "homepage"),
    "phone": ("phonenumber", "telephone", "tel", "contactnumber", "mobile"),
    "industry": ("sector", "vertical", "category"),
    "employee_count": ("employees", "headcount", "numberofemployees", "staff",
                       "companysize", "size"),
    "annual_revenue": ("revenue", "turnover", "annualturnover", "sales",
                       "yearlyrevenue"),
    "description": ("notes", "about", "summary", "comments"),
    "amount": ("value", "dealvalue", "dealamount", "price", "worth"),
    "expected_close_date": ("closedate", "expectedclose", "closingdate",
                            "estimatedclosedate"),
    "pipeline": ("pipelinename", "funnel"),
    "stage": ("stagename", "dealstage", "status", "phase"),
    "owner": ("ownername", "assignedto", "accountmanager", "salesrep", "rep"),
}

# ── notes (CRM-413) ──────────────────────────────────────────────────────────
# A note is not attribute-driven: it has one body and a set of records it hangs
# off, and `crm_attributes` has no `note` object type. So the map screen has no
# definitions to suggest from, and without these every column of a note file
# would come back unmapped. Same `internal_name`/`label` shape `suggest_mapping`
# reads off a real attribute definition.
NOTE_COLUMNS: Tuple[Dict[str, str], ...] = (
    {"internal_name": "body", "label": "Note"},
    {"internal_name": "company_id", "label": "Company id"},
    {"internal_name": "deal_id", "label": "Lead id"},
    {"internal_name": "contact_id", "label": "Contact id"},
    {"internal_name": "contact_email", "label": "Contact email"},
)

# What to call each note column in an error message. "contact_email "x" doesn't
# match any contact" reads like a bug; "Contact email" reads like the dropdown
# the user chose on the map screen, which is what they have to go and fix.
NOTE_LABELS: Dict[str, str] = {column["internal_name"]: column["label"]
                               for column in NOTE_COLUMNS}

# ⚠️ NOT `SYNONYMS`. There, `company_id` claims the bare header "id" — right
# for a company export being re-imported, wrong for a note file, where "id" is
# far more likely the note's own id, and mapping it to a company reference would
# attach every note to nothing and report an unresolved reference per row.
# `description` points at `body` here and at a company's own description there,
# for the same reason: the target set is different, so the synonyms are too.
NOTE_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "body": ("note", "notes", "comment", "comments", "description", "text",
             "content", "message"),
    "company_id": ("companyid", "accountid", "organisationid", "organizationid"),
    "deal_id": ("dealid", "opportunityid"),
    "contact_id": ("contactid", "personid"),
    "contact_email": ("email", "contactemail", "emailaddress", "personemail"),
}


def suggest_mapping(headers: Sequence[str],
                    attributes: Sequence[Any],
                    synonyms: Optional[Dict[str, Tuple[str, ...]]] = None,
                    ) -> List[Optional[str]]:
    """One suggested target per column, BY POSITION. ``None`` = unmapped.

    Positional because duplicate headers are common and a dict keyed by header
    text would silently drop one of them.

    Matching is exact-after-normalisation only — on the internal name, the
    label, or a known synonym. No fuzzy scoring: the user is shown a screen of
    pre-filled dropdowns and will trust them, so a plausible-but-wrong guess
    costs more than an empty one they have to fill in.

    Each target is used at most once. Two columns both called "Name" map the
    first and leave the second for the user, rather than quietly fighting over
    the same field.

    ``synonyms`` defaults to the company/deal/contact table. Notes pass
    ``NOTE_SYNONYMS``, because the same header means different things in a note
    file — see the note on that constant.
    """
    table = SYNONYMS if synonyms is None else synonyms
    by_key: Dict[str, str] = {}
    for attribute in attributes or []:
        internal = _get(attribute, "internal_name")
        if not internal:
            continue
        for candidate in (internal, _get(attribute, "label")):
            key = normalise_header(candidate)
            if key:
                by_key.setdefault(key, internal)
        for synonym in table.get(internal, ()):
            by_key.setdefault(synonym, internal)

    taken: set = set()
    out: List[Optional[str]] = []
    for header in headers or []:
        target = by_key.get(normalise_header(header))
        if target and target not in taken:
            taken.add(target)
            out.append(target)
        else:
            out.append(None)
    return out


def column_samples(headers: Sequence[str], rows: Sequence[Sequence[Any]],
                   limit: int = 3) -> List[List[str]]:
    """Up to ``limit`` non-empty example values per column, for the map screen.

    Samples are what let someone recognise a badly-named column: "Field_7" says
    nothing, but "Field_7 · acme.com, globex.io" is obviously the domain.
    """
    out: List[List[str]] = []
    for index in range(len(headers or [])):
        seen: List[str] = []
        for row in rows or []:
            if index >= len(row):
                continue
            value = str(row[index] or "").strip()
            if value and value not in seen:
                seen.append(value)
            if len(seen) >= limit:
                break
        out.append(seen)
    return out


# ── per-row work ─────────────────────────────────────────────────────────────
def row_to_values(row: Sequence[Any], mapping: Sequence[Optional[str]]
                  ) -> Dict[str, str]:
    """Apply a positional mapping to one row.

    A row shorter than the header is normal — trailing empty cells are often
    simply absent — so a missing column is an empty value, not an error.

    When two columns map to the same target (which the UI allows, even if
    ``suggest_mapping`` will not produce it), the FIRST NON-EMPTY one wins.
    Last-wins would let a trailing empty duplicate column blank out real data.
    """
    out: Dict[str, str] = {}
    for index, target in enumerate(mapping or []):
        if not target:
            continue
        cell = row[index] if index < len(row) else None
        # str(None) is the string "None", which would land in the database as a
        # company called None. Empty-check before stringifying, always.
        value = "" if cell is None else str(cell).strip()
        if value and not out.get(target):
            out[target] = value
        out.setdefault(target, "")
    return out


def dedupe_value(values: Dict[str, Any], key: Optional[str]) -> Optional[str]:
    """The comparable form of a row's dedupe key, or None if it has none.

    ``domain`` is normalised the same way ``dedupe.normalize_domain`` does, so
    ``https://WWW.Acme.com/`` in the file matches ``acme.com`` in the database.
    Comparing the raw strings would make the update-existing option quietly do
    nothing for most real files.
    """
    if not key:
        return None
    raw = str(values.get(key) or "").strip()
    if not raw:
        return None
    if key == "domain":
        from app.services.dedupe import normalize_domain

        return normalize_domain(raw) or None
    if key == "name":
        from app.services.dedupe import normalize_company_name

        return normalize_company_name(raw) or None
    if key == "email":
        # CRM-412. The same normalisation `contact_rules.normalize_email` does:
        # lowercase and trim, and deliberately NOT stripping dots or plus-tags,
        # because `a.b@x.com` and `ab@x.com` are two people. The generic
        # `raw.lower()` below happens to agree today; saying it here means a
        # change to that fallback cannot silently change the dedupe key the
        # unique index is built on.
        return raw.strip().lower() or None
    if key in ("company_id", "id", "deal_id", "contact_id"):
        # An ObjectId's hex is case-insensitive in practice but is compared as
        # a string downstream, so it is normalised the same way every time —
        # and NOT lowercased into something that no longer round-trips.
        return raw.strip() or None
    return raw.lower()


def summarise(counters: Dict[str, int]) -> str:
    """A one-line human summary, e.g. "18 created, 4 updated, 2 skipped"."""
    parts = []
    for key in ("created", "updated", "skipped", "failed"):
        value = counters.get(key) or 0
        if value:
            parts.append(f"{value:,} {key}")
    return ", ".join(parts) or "nothing to do"


def error_csv(errors: Iterable[Dict[str, Any]], headers: Sequence[str]
              ) -> str:
    """The rejected-rows report. CRM-231.

    The ORIGINAL row number is the first column and the original cells follow,
    so the file opens beside the source and lines up. A report that only says
    "4 rows failed: invalid date" cannot be acted on.
    """
    buffer = io.StringIO()
    # Excel needs the BOM to read a UTF-8 CSV as UTF-8. Without it, every
    # accented name in the error report is mojibake — in a file whose entire
    # purpose is to be read by a person.
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow(["row_number", "error", *list(headers or [])])
    for error in errors or []:
        writer.writerow([
            error.get("row_number", ""),
            error.get("message", ""),
            *[("" if cell is None else str(cell)) for cell in (error.get("row") or [])],
        ])
    return buffer.getvalue()


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def iter_data_rows(rows: Sequence[Sequence[Any]]
                   ) -> Iterator[Tuple[int, Sequence[Any]]]:
    """``(file_row_number, row)`` for every non-blank row."""
    for index, row in enumerate(rows or []):
        if is_blank_row(row):
            continue
        yield file_row_number(index), row
