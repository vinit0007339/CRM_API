"""Role-based access control (RBAC) — pure-Python permission logic.

Doc requirements (Enterprise + Authentication & identity):
* "Teams & role-based permissions."
* "Role-based access control with custom roles."
* "Object-level permissions."

Pure-Python (stdlib only). Defines the permission catalogue, the built-in roles
and their grants, custom-role resolution, and object-scoped checks. The route
layer persists team members / custom roles and calls ``has_permission`` to gate
actions. Fully unit-testable with no DB.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

# ── Permission catalogue ──────────────────────────────────────────────────────
# Format: "<resource>:<action>". A role granting "*" has everything.
# Simplified to read/write per resource. The previously-separate action
# variants (send / approve / export / delete) are NOT distinct permissions —
# they are covered by ``<resource>:write`` (e.g. sending or approving a
# campaign requires ``campaigns:write``; exporting or deleting contacts
# requires ``contacts:write``). This keeps roles easy to reason about.
PERMISSIONS = {
    "contacts:read", "contacts:write",
    "lists:read", "lists:write",
    "campaigns:read", "campaigns:write",
    "segments:read", "segments:write",
    "flows:read", "flows:write",
    "templates:read", "templates:write",
    "analytics:read",
    "billing:read", "billing:write",
    "team:read", "team:write",
    "settings:read", "settings:write",
    "apikeys:read", "apikeys:write",
    "inbox:read", "inbox:write",

    # ── CRM module (Companies, Deals, Pipelines, Tasks) ───────────────────
    "companies:read", "companies:write",
    "deals:read", "deals:write",
    "pipelines:read", "pipelines:write",
    "crm_tasks:read", "crm_tasks:write",

    # Deliberate exception to the "<resource>:<action>" read/write shape.
    # Granted per-member via ``TeamMember.extra_grants``; lets a user edit or
    # delete CRM records owned by somebody else. Read is always account-wide,
    # so this only ever widens write/delete. See CRM plan §9.3.
    "crm:manage_others",
}

WILDCARD = "*"

# ── Built-in roles ────────────────────────────────────────────────────────────
BUILTIN_ROLES: Dict[str, Set[str]] = {
    "owner": {WILDCARD},
    "admin": {WILDCARD},
    "editor": {
        "contacts:read", "contacts:write", "lists:read", "lists:write",
        "campaigns:read", "campaigns:write",
        "segments:read", "segments:write", "flows:read", "flows:write",
        "templates:read", "templates:write", "analytics:read", "settings:read",
        # CRM: full access to records, but pipeline *structure* stays with
        # owner/admin — reshaping stages changes everyone's board and
        # invalidates historical reporting.
        "companies:read", "companies:write",
        "deals:read", "deals:write",
        "crm_tasks:read", "crm_tasks:write",
        "pipelines:read",
        "inbox:read", "inbox:write",
    },
    "analyst": {
        "contacts:read", "lists:read", "campaigns:read", "segments:read",
        "flows:read", "templates:read", "analytics:read",
        "companies:read", "deals:read", "pipelines:read", "crm_tasks:read",
        # Read, not write: an analyst studies what customers said, and
        # answering them is somebody else's job.
        "inbox:read",
    },
    "support": {
        # Support can view and fix individual contact records, but list
        # creation/management is a separate, more privileged action (mirrors
        # campaigns having their own "campaigns:write" permission).
        "contacts:read", "contacts:write", "lists:read",
        "campaigns:read", "analytics:read",
        # CRM: read everything, and work the task queue — but not the records.
        "companies:read", "deals:read", "pipelines:read",
        "crm_tasks:read", "crm_tasks:write",
        # Answering customer mail is the whole of this role's job, so support
        # gets write here even though it does not get campaigns:write.
        "inbox:read", "inbox:write",
    },
    "viewer": {
        "contacts:read", "lists:read", "campaigns:read", "segments:read",
        "flows:read", "templates:read", "analytics:read", "inbox:read",
        "companies:read", "deals:read", "pipelines:read", "crm_tasks:read",
    },
}

BUILTIN_ROLE_NAMES = set(BUILTIN_ROLES.keys())


def normalize_permissions(perms: Iterable[str]) -> Set[str]:
    """Validate + dedupe a permission set for a custom role.

    ``"*"`` is allowed (full access). Unknown permission strings raise so a
    typo can't silently grant/deny.
    """
    out: Set[str] = set()
    for p in perms:
        p = (p or "").strip()
        if not p:
            continue
        if p == WILDCARD or p in PERMISSIONS:
            out.add(p)
        else:
            raise ValueError(f"Unknown permission {p!r}")
    return out


def resolve_role_permissions(
    role: str,
    custom_roles: Optional[Dict[str, Iterable[str]]] = None,
    builtin_overrides: Optional[Dict[str, Iterable[str]]] = None,
) -> Set[str]:
    """Return the permission set for a role name.

    Resolution order: ``builtin_overrides`` (admin-edited built-in roles, stored
    in the DB) → code-default built-in roles → ``custom_roles`` (account-defined).
    An unknown role resolves to no permissions (deny-by-default).
    """
    if builtin_overrides and role in builtin_overrides:
        return set(builtin_overrides[role])
    if role in BUILTIN_ROLES:
        return set(BUILTIN_ROLES[role])
    if custom_roles and role in custom_roles:
        return set(custom_roles[role])
    return set()


def has_permission(
    role: str,
    permission: str,
    *,
    custom_roles: Optional[Dict[str, Iterable[str]]] = None,
    extra_grants: Optional[Iterable[str]] = None,
    denials: Optional[Iterable[str]] = None,
    builtin_overrides: Optional[Dict[str, Iterable[str]]] = None,
) -> bool:
    """Does ``role`` grant ``permission``?

    * Wildcard ``"*"`` grants everything.
    * ``extra_grants`` are per-member additional grants on top of the role.
    * ``denials`` are explicit per-member removals that override grants
      (object-level "can't export contacts" style restriction).
    * ``builtin_overrides`` lets admin-edited built-in role definitions win.
    """
    if permission in (denials or set()):
        return False
    granted = resolve_role_permissions(role, custom_roles, builtin_overrides)
    granted |= set(extra_grants or set())
    if WILDCARD in granted:
        # Wildcard still respects explicit denials (handled above).
        return True
    return permission in granted


def assignable_roles(actor_role: str) -> Set[str]:
    """Which roles an actor may assign to others.

    Only owners/admins can assign roles, and nobody can grant a role above their
    own level (owner is top). Prevents privilege escalation.
    """
    if actor_role not in ("owner", "admin"):
        return set()
    if actor_role == "owner":
        return BUILTIN_ROLE_NAMES
    # admin can assign anything except owner
    return BUILTIN_ROLE_NAMES - {"owner"}
