"""Record-level CRM authorisation. CRM-016.

RBAC (``api/services/engage/rbac.py``) answers "may this role touch deals at
all?". This module answers "may this *user* touch *this* deal?".

The model is ownership, not row-level ACLs — the same shape Brevo uses:

* READ is account-wide. Anyone with ``<resource>:read`` sees every record in
  the account. Scoping reads by owner would break team visibility, pipeline
  forecasting and every report, and there is no evidence Brevo does it.
* WRITE and DELETE additionally require that the caller owns the record, holds
  the ``crm:manage_others`` grant, or is the account owner.

Stdlib-only, so it is unit-testable without a database.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

MANAGE_OTHERS = "crm:manage_others"

# Rendered into the failure message so it names the object the user tried to
# touch rather than saying "record".
_LABELS = {
    "company": "companies",
    "deal": "deals",
    "task": "tasks",
    "pipeline": "pipelines",
}


def _same(a: Any, b: Any) -> bool:
    """ObjectId-vs-string tolerant identity check."""
    if a is None or b is None:
        return False
    return str(a) == str(b)


def can_read(*, has_read_permission: bool) -> bool:
    """Read is account-wide — ownership is irrelevant. Here for symmetry, so
    call sites read consistently and the rule is documented in one place.
    """
    return bool(has_read_permission)


def can_modify(
    *,
    has_write_permission: bool,
    actor_user_id: Any,
    record_owner_id: Any,
    is_account_owner: bool = False,
    has_manage_others: bool = False,
) -> bool:
    """May this user edit or delete this record?"""
    if not has_write_permission:
        return False
    if is_account_owner or has_manage_others:
        return True
    # An unowned record is editable by anyone who can write — otherwise an
    # imported record with no owner would be locked for everybody.
    if record_owner_id is None:
        return True
    return _same(actor_user_id, record_owner_id)


def authorize_modify(
    *,
    has_write_permission: bool,
    actor_user_id: Any,
    record_owner_id: Any,
    is_account_owner: bool = False,
    has_manage_others: bool = False,
    resource: str = "deal",
) -> Tuple[bool, Optional[str]]:
    """``(allowed, message)``. The message is user-facing — surface it verbatim.

    Note the house convention: permission failures are returned as HTTP 200
    with ``status="failure"``, so this message ends up rendered inline.
    """
    plural = _LABELS.get(resource, resource + "s")
    if not has_write_permission:
        return False, f"You do not have permission to edit {plural}."
    if can_modify(
        has_write_permission=has_write_permission,
        actor_user_id=actor_user_id,
        record_owner_id=record_owner_id,
        is_account_owner=is_account_owner,
        has_manage_others=has_manage_others,
    ):
        return True, None
    return False, f"You can only edit {plural} you own."


def can_delete(**kwargs) -> bool:
    """Delete follows the same rule as edit."""
    return can_modify(**kwargs)
