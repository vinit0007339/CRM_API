"""Association pair normalisation — pure logic. CRM-009.

Every CRM relationship (company<->contact, company<->deal, deal<->contact) is
stored as ONE row in ``crm_associations``, in a canonical direction. Without
normalising before the write, a unique index is worthless: "deal D -> contact C"
and "contact C -> deal D" would both insert happily and the pair would be
duplicated.

Canonical direction is decided by a fixed precedence:

    company  <  deal  <  contact

So a deal<->contact link is always stored ``from=deal, to=contact``; a
company<->deal link always ``from=company, to=deal``.

This ordering matches the member order of ``AssociationEntity`` and must not be
changed — existing rows would no longer match newly-normalised lookups.

Stdlib-only. No Beanie, no Motor, no pydantic.
"""
from __future__ import annotations

from typing import Any, Tuple

# Lower number wins the "from" slot.
_PRECEDENCE = {"company": 0, "deal": 1, "contact": 2}


class InvalidAssociation(ValueError):
    """Raised for an unknown entity type or a self-referential pair."""


def _type_value(entity_type: Any) -> str:
    """Accept either the enum member or its raw string value."""
    return getattr(entity_type, "value", entity_type)


def normalize_pair(
    from_type: Any,
    from_id: Any,
    to_type: Any,
    to_id: Any,
) -> Tuple[str, Any, str, Any]:
    """Return the pair in canonical storage order.

    >>> normalize_pair("contact", "c1", "deal", "d1")
    ('deal', 'd1', 'contact', 'c1')
    >>> normalize_pair("company", "co1", "contact", "c1")
    ('company', 'co1', 'contact', 'c1')

    Raises:
        InvalidAssociation: unknown entity type, or a record linked to itself.
    """
    a_type, b_type = _type_value(from_type), _type_value(to_type)

    for t in (a_type, b_type):
        if t not in _PRECEDENCE:
            raise InvalidAssociation(f"Unknown association entity type {t!r}")

    if a_type == b_type and str(from_id) == str(to_id):
        raise InvalidAssociation("A record cannot be associated with itself")

    if _PRECEDENCE[a_type] <= _PRECEDENCE[b_type]:
        return a_type, from_id, b_type, to_id
    return b_type, to_id, a_type, from_id


def pair_filter(account_id: Any, from_type: Any, from_id: Any,
                to_type: Any, to_id: Any) -> dict:
    """Mongo filter for one association, normalised. Use for both read and write."""
    ft, fid, tt, tid = normalize_pair(from_type, from_id, to_type, to_id)
    return {
        "created_by": account_id,
        "from_type": ft,
        "from_id": fid,
        "to_type": tt,
        "to_id": tid,
    }


def counterpart(row: dict, entity_type: Any, entity_id: Any) -> Tuple[str, Any]:
    """Given a stored row and one side of it, return the other side.

    Lets callers walk an association without caring which slot they're in.
    """
    et = _type_value(entity_type)
    if row.get("from_type") == et and str(row.get("from_id")) == str(entity_id):
        return row["to_type"], row["to_id"]
    return row["from_type"], row["from_id"]
