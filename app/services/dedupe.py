"""Company duplicate detection. CRM-017.

Brevo keys deduplication on the company domain and *hard-blocks* creating a
second company with a domain already in use. We detect the same way but treat
it as a warning: subsidiaries, franchises and regional entities legitimately
share a parent domain, and a hard block generates support load. The create
endpoint returns ``duplicate_warning`` and the UI offers "Open existing" or
"Create anyway" (CRM plan §3.3).

Three confidence tiers:

* ``high``   — identical normalised domain
* ``medium`` — identical normalised name, where at least one side has no domain
* ``low``    — normalised names similar above SIMILARITY_THRESHOLD (suggestion only)

Stdlib-only (``re`` + ``difflib``). No DB, no third-party fuzzy-match library.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple

HIGH, MEDIUM, LOW = "high", "medium", "low"

# Only surfaced as a suggestion, never used to auto-merge.
SIMILARITY_THRESHOLD = 0.90

# Stripped from the end of a company name before comparison. Order matters:
# longer forms first so "private limited" wins over "limited".
_LEGAL_SUFFIXES = (
    "private limited", "pvt limited", "pvt ltd", "pvt. ltd.", "p ltd",
    "limited liability company", "limited", "ltd", "llc", "llp", "plc",
    "incorporated", "inc", "corporation", "corp", "company", "co",
    "gmbh", "ag", "bv", "nv", "sa", "sas", "srl", "spa", "oy", "ab", "as",
)

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MULTI_SPACE = re.compile(r"\s+")


def normalize_domain(value: Optional[str]) -> Optional[str]:
    """Reduce any URL-ish string to a bare lowercase host.

    ``HTTPS://WWW.Acme.com:8443/about?x=1`` -> ``acme.com``

    Returns None for empty input or anything with no host left after stripping.
    """
    if not value:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    v = _SCHEME.sub("", v)          # scheme
    v = v.lstrip("/")               # protocol-relative "//acme.com/x"
    v = v.split("/", 1)[0]          # path
    v = v.split("?", 1)[0]          # stray query
    v = v.split("#", 1)[0]          # fragment
    v = v.split("@")[-1]            # userinfo, or an email address
    v = v.split(":", 1)[0]          # port
    v = v.strip().strip(".")        # trailing dot (FQDN root)
    if v.startswith("www."):
        v = v[4:]
    if not v or "." not in v:
        return None
    return v


def normalize_company_name(value: Optional[str]) -> str:
    """Casefold, drop legal suffixes and punctuation, collapse whitespace.

    ``Acme Private Limited.`` and ``ACME  Pvt. Ltd`` both -> ``acme``.
    """
    if not value:
        return ""
    v = str(value).strip().lower()
    v = _NON_ALNUM.sub(" ", v)
    v = _MULTI_SPACE.sub(" ", v).strip()
    # Suffixes can stack: "Acme Holdings Pvt Ltd Inc"
    changed = True
    while changed and v:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if v.endswith(" " + suffix):
                v = v[: -(len(suffix) + 1)].strip()
                changed = True
                break
    return v


def similarity(a: Optional[str], b: Optional[str]) -> float:
    """0.0-1.0 similarity of two *normalised* company names."""
    na, nb = normalize_company_name(a), normalize_company_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def match_confidence(
    left_name: Optional[str],
    left_domain: Optional[str],
    right_name: Optional[str],
    right_domain: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """``(confidence, matched_on)`` for a candidate pair, or ``(None, None)``."""
    ld, rd = normalize_domain(left_domain), normalize_domain(right_domain)
    if ld and rd:
        if ld == rd:
            return HIGH, "domain"
        # Both have domains and they differ — deliberately do NOT fall through
        # to a name check. Two companies with distinct domains are distinct.
        return None, None

    ln, rn = normalize_company_name(left_name), normalize_company_name(right_name)
    if not ln or not rn:
        return None, None
    if ln == rn:
        return MEDIUM, "name"
    if similarity(left_name, right_name) >= SIMILARITY_THRESHOLD:
        return LOW, "name"
    return None, None


def find_duplicates(
    name: Optional[str],
    domain: Optional[str],
    candidates: Iterable[Dict[str, Any]],
    *,
    exclude_id: Any = None,
    min_confidence: str = LOW,
) -> List[Dict[str, Any]]:
    """Rank ``candidates`` against one company. Highest confidence first.

    ``candidates`` are plain dicts with at least ``id``/``_id``, ``name`` and
    ``domain`` — the caller does the (indexed) query, this does the matching.
    """
    order = {HIGH: 0, MEDIUM: 1, LOW: 2}
    floor = order.get(min_confidence, 2)
    out: List[Dict[str, Any]] = []
    for c in candidates:
        cid = c.get("id", c.get("_id"))
        if exclude_id is not None and cid is not None and str(cid) == str(exclude_id):
            continue
        conf, matched_on = match_confidence(name, domain, c.get("name"), c.get("domain"))
        if conf is None or order[conf] > floor:
            continue
        out.append({"id": cid, "name": c.get("name"), "domain": c.get("domain"),
                    "confidence": conf, "matched_on": matched_on})
    out.sort(key=lambda r: (order[r["confidence"]], str(r["name"] or "")))
    return out


def derive_name_fields(name):
    """``{"name", "name_normalised"}`` — the ONLY way either is written.

    Two fields expressing one fact drift the moment something sets one without
    the other, and the failure is silent: duplicate detection simply stops
    finding a record, and nobody reports "I did not see a warning".

    So every write path goes through here, ``CompanyModel``'s validator
    re-derives it on construction, and
    ``scripts/backfill_company_name_normalised.py`` re-checks the whole
    collection.
    """
    cleaned = (name or "").strip()
    return {"name": cleaned, "name_normalised": normalize_company_name(cleaned) or None}
