"""Response assembly for the CRM API. CRM-032.

Pure functions that turn model documents + pre-fetched lookup tables into the
response payloads. Two reasons this is a module rather than inline handler code:

* **N+1 avoidance is a shape problem, not a query problem** (NFR6). The handler
  collects every owner/tag/company id in one pass, fetches them in one query
  each, and passes the resulting index down here. Expansion then costs nothing.
  Keeping that split explicit is what stops someone "just fetching the owner"
  inside a loop later.
* Route handlers can't be executed in this environment (no fastapi/motor
  available), so anything left inline is untestable. This is testable.

Everything here takes plain dicts / model objects and returns plain dicts.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


def _val(obj: Any, field: str, default: Any = None) -> Any:
    """Read a field from a Beanie model or a plain dict, indifferently."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def _sid(value: Any) -> Optional[str]:
    return None if value is None else str(value)


# ── batching helpers ─────────────────────────────────────────────────────────
def collect_ids(rows: Iterable[Any], *fields: str) -> Set[str]:
    """Every non-null id across ``fields`` on ``rows``, as strings.

    Feeds one batched lookup per related collection — call this before the
    fetch, never during the render.
    """
    out: Set[str] = set()
    for row in rows or []:
        for field in fields:
            value = _val(row, field)
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                out.update(str(v) for v in value if v is not None)
            else:
                out.add(str(value))
    return out


def index_by_id(rows: Iterable[Any], id_field: str = "id") -> Dict[str, Any]:
    """``{str(id): row}`` lookup table."""
    index: Dict[str, Any] = {}
    for row in rows or []:
        rid = _val(row, id_field) or _val(row, "_id")
        if rid is not None:
            index[str(rid)] = row
    return index


# ── reference expansion ──────────────────────────────────────────────────────
def user_ref(user: Any) -> Optional[Dict[str, Any]]:
    """Expanded owner/actor. Falls back to the email when no name is set, so
    the UI never renders an empty avatar chip.
    """
    if user is None:
        return None
    first = _val(user, "first_name") or ""
    last = _val(user, "last_name") or ""
    name = (f"{first} {last}").strip() or _val(user, "username") or _val(user, "email")
    return {
        "id": _val(user, "id") or _val(user, "_id"),
        "name": name,
        "email": _val(user, "email"),
        "avatar_url": _val(user, "picture_url"),
    }


def tag_refs(tag_ids: Optional[Sequence[Any]], tag_index: Dict[str, Any]) -> List[Dict]:
    """Expand tag ids, silently dropping ones that no longer exist.

    A deleted tag must not break a company row — it just stops being shown.
    """
    out = []
    for tid in tag_ids or []:
        tag = tag_index.get(str(tid))
        if tag is None:
            continue
        out.append({
            "id": _val(tag, "id") or _val(tag, "_id"),
            "name": _val(tag, "name"),
            "color": _val(tag, "color"),
        })
    return out


def company_ref(company: Any) -> Optional[Dict[str, Any]]:
    if company is None:
        return None
    return {
        "id": _val(company, "id") or _val(company, "_id"),
        "name": _val(company, "name"),
        "domain": _val(company, "domain"),
    }


def contact_ref(contact: Any, role: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": _val(contact, "id") or _val(contact, "_id"),
        "email": _val(contact, "email"),
        "first_name": _val(contact, "first_name"),
        "last_name": _val(contact, "last_name"),
        "role": role,
    }


# ── company payloads ─────────────────────────────────────────────────────────
def company_payload(
    company: Any,
    *,
    owner_index: Dict[str, Any] = None,
    tag_index: Dict[str, Any] = None,
    linked_contacts: Optional[Dict[str, Any]] = None,
    linked_deals: Optional[Dict[str, Any]] = None,
    duplicate_warning: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One company, ready for ``CompanyDetails.model_validate``.

    ``linked_*`` are only supplied on the single-record endpoint — the list
    endpoint leaves them None so a page of 50 companies doesn't fan out into
    100 association queries.
    """
    owner_index = owner_index or {}
    tag_index = tag_index or {}

    payload = company.model_dump(by_alias=True) if hasattr(company, "model_dump") else dict(company)
    payload["owner"] = user_ref(owner_index.get(_sid(_val(company, "owner_id"))))
    payload["tags"] = tag_refs(_val(company, "tag_ids"), tag_index)

    if linked_contacts is not None:
        payload["linked_contacts"] = linked_contacts
    if linked_deals is not None:
        payload["linked_deals"] = linked_deals
    if duplicate_warning is not None:
        payload["duplicate_warning"] = duplicate_warning
    return payload


# JSON-safe scalars. Everything else in an UNTYPED container has to be coerced
# before it reaches a response — see json_safe.
_PASSTHROUGH = (str, int, float, bool, datetime, date)


def json_safe(value: Any) -> Any:
    """Coerce a value so pydantic can serialise it inside an untyped container.

    THE TRAP THIS EXISTS FOR.

    ``PyObjectId`` carries a pydantic serializer (``str``, json mode), so a
    field *typed* as PyObjectId serialises fine. A ``Dict[str, Any]`` has no
    schema for its contents, so pydantic falls back to its "any" serializer,
    meets a raw ``bson.ObjectId`` and raises::

        PydanticSerializationError: Unable to serialize unknown type:
        <class 'bson.objectid.ObjectId'>

    -> HTTP 500. It hit ``GET /crm/deals/{id}/activities``, because a
    ``stage_changed`` activity stores ``from_stage_id``/``to_stage_id`` in its
    ``metadata`` dict, and those come back from Mongo as raw ObjectIds. The
    same trap sits in ``LinkedCollection.data`` on the company detail response.

    Anything unrecognised becomes ``str(value)`` — right for ObjectId, UUID and
    Decimal128, and for anything else a string beats a 500. Enums use their
    value, not ``repr``.
    """
    if value is None:
        return value
    # Enum FIRST. This codebase's enums subclass str, so an isinstance(str)
    # check above this one would send `AttributeType.CURRENCY` instead of
    # `currency` — the picker would show the repr and every value comparison
    # downstream would miss.
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, str):
        # Normalise str SUBCLASSES to plain str. Real bson.ObjectId is not a
        # str and falls through to the str() at the bottom, but plenty of id
        # wrappers are str subclasses, and letting one through means the wire
        # type depends on which class happened to build the value.
        return value if type(value) is str else str(value)
    if isinstance(value, _PASSTHROUGH):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def public_dump(doc: Any) -> Dict[str, Any]:
    """A Beanie document as the wire sees it: `id`, never `_id`.

    `model_dump(by_alias=True)` on a Document emits `_id`, which is right for
    Mongo and wrong for a client — every CRM screen reads `.id`. Use this for
    any response that returns raw dicts instead of going through a `*Details`
    schema (the schemas handle it themselves via `serialization_alias`).
    """
    data = doc.model_dump(by_alias=True) if hasattr(doc, "model_dump") else dict(doc)
    if "_id" in data:
        data["id"] = data.pop("_id")
    # These endpoints declare a bare ResponseSchema, so nothing downstream will
    # coerce ObjectIds for us.
    return json_safe(data)


def linked_collection(rows: Sequence[Any], total: int, renderer) -> Dict[str, Any]:
    """``{"data": [...], "total": N}`` — the truncated-set shape.

    ``total`` is the true count, not ``len(rows)``: the detail page shows the
    first 20 linked records but has to say "of 43".
    """
    return {"data": [json_safe(renderer(r)) for r in rows], "total": int(total)}


def pick_duplicate_warning(duplicates: Sequence[Dict[str, Any]]) -> Optional[Dict]:
    """The single best duplicate to warn about on create.

    ``dedupe.find_duplicates`` already sorts by confidence, so this is the
    first one — but only if it clears the "worth interrupting for" bar. A
    low-confidence fuzzy name match is a suggestion, not a warning, and
    interrupting a create for it would train users to click through.
    """
    for row in duplicates or []:
        if row.get("confidence") in ("high", "medium"):
            return {
                "id": row.get("id"),
                "name": row.get("name"),
                "confidence": row.get("confidence"),
                "matched_on": row.get("matched_on"),
            }
    return None


# ── update payload handling ──────────────────────────────────────────────────
# Fields a PATCH may set. Everything else — tenancy, timestamps, rollup
# counters, denormalised mirrors, association arrays — is server-owned. The
# schema already excludes them; this is the second lock, because a schema is
# easy to widen by accident and this list is not.
COMPANY_UPDATABLE = frozenset({
    "name", "domain", "website", "linkedin_url", "industry", "employee_count",
    "annual_revenue", "currency", "phone", "phone_country_code", "address",
    "description", "owner_id", "tag_ids", "custom_fields", "is_archived",
})

DEAL_UPDATABLE = frozenset({
    "name", "amount", "currency", "expected_close_date", "probability",
    "owner_id", "description", "source", "tag_ids", "custom_fields",
})


def allowed_updates(payload: Dict[str, Any], allowed: frozenset) -> Dict[str, Any]:
    """Drop unset (None) and server-owned fields from a PATCH payload.

    None means "not supplied" throughout the CRM API — there is no way to null
    a field via PATCH, which is deliberate: clearing is an explicit action, not
    an accident of a partially-populated form.
    """
    return {
        k: v for k, v in (payload or {}).items()
        if k in allowed and v is not None
    }


def changed_fields(before: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Only the updates that actually differ from the current values.

    Stops a no-op save from writing an "updated" activity that says nothing.
    """
    return {
        k: v for k, v in (updates or {}).items()
        if _val(before, k) != v
    }


# ── bulk selection ───────────────────────────────────────────────────────────
def resolve_bulk_selection(
    ids: Optional[Sequence[Any]], filters: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Normalise "these ids" vs "everything matching this filter".

    The second form is what powers "select all 1,284 matching" without the
    client enumerating ids.

    Raises:
        ValueError: neither or both supplied — ambiguous, so refuse rather than
            silently picking one.
    """
    has_ids = bool(ids)
    has_filters = bool(filters)
    if has_ids and has_filters:
        raise ValueError("Pass either ids or filters for a bulk action, not both.")
    if not has_ids and not has_filters:
        raise ValueError("Select some records, or supply a filter.")
    return {"mode": "ids", "ids": list(ids)} if has_ids else {"mode": "filters", "filters": filters}
