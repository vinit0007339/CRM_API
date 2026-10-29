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
# ⚠️ KEEP IN STEP WITH `AssociationEntity`. This module is stdlib-only by
# design — "No Beanie, no Motor, no pydantic" — so it cannot import the enum
# and holds its own copy of the ordering. `scripts/check_invariants.py` asserts
# the two agree, and it caught exactly this entry being missed when
# `crm_contact` was added.
#
# `crm_contact` ranks last, so a company/deal always wins the `from` slot and a
# CRM contact normalises into `to` — the same shape as a platform contact.
_PRECEDENCE = {"company": 0, "deal": 1, "contact": 2, "crm_contact": 3}


class InvalidAssociation(ValueError):
    """Raised for an unknown entity type or a self-referential pair."""


def _type_value(entity_type: Any) -> str:
    """Accept either the enum member or its raw string value."""
    return getattr(entity_type, "value", entity_type)


def person_entity(unified: bool) -> str:
    """Which association type a CRM person is, on this account's path.

    Phase 6 of `docs/contact-unification-plan.md`. **The one place that
    decision is made**, for the same reason `contact_scope.unified` is the one
    place the flag is read: a writer that disagrees by one branch produces
    rows nothing can find.

    ⚠️ `AssociationEntity.CRM_CONTACT` IS NOT REMOVED, AND MUST NOT BE. Its
    POSITION in the enum defines the canonical direction a pair is stored in
    (see this module's `_PRECEDENCE` and the enum's own warning). Deleting the
    member would re-order the ones after it and orphan every row already
    written. It becomes legacy — read, never written — which is a different
    thing from gone.

    ⚠️ AND THE OLD ROWS STILL RESOLVE. A deal linked to a CRM person before
    the cutover holds `to_type="crm_contact"`; the detail routes read the type
    that matches the account's current path, and phase 7's migration is what
    rewrites the rows. Until it runs, an account that is flipped on sees its
    pre-cutover links disappear from the page — which is why the flag is
    per-account and the migration is not optional.
    """
    return "contact" if unified else "crm_contact"


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
