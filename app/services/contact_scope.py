"""Account scoping for the PLATFORM contact. CRM-P2-000, phase 1.

WHY THIS EXISTS. ``Contact.email`` is ``Indexed(unique=True)`` — globally
unique across every account — and ``owner_ids`` lets one document be shared
between accounts. So a query that names an email and no account returns an
ARBITRARY tenant's row, and a write to it lands on a record this account may
never have created.

That is not theoretical. It is how a CSV import comes to overwrite another
account's contact names, how a webhook import re-subscribes somebody a
different account unsubscribed, and how an SMS import rewrites another
account's consent and stamps its own IP as the provenance.

THE ASYMMETRY THIS CLOSES. The CRM wires ``crm/queries.assert_account_scoped``
into its CRUD base (``app/db/crud/main/crm/base.py``), so every ``crm_*`` query
is scoped by construction, and the module has had no tenancy bug since. The
platform contact has the same predicate retyped in a dozen places and a defect
list to show for it. This module is that helper for ``contacts``.

⚠️ THE PREDICATE IS ``created_by`` **OR** ``owner_ids``, AND BOTH HALVES ARE
REQUIRED. ``created_by`` is who made the row; ``owner_ids`` is everyone who has
since been granted it, which is how a contact imported by an admin is visible
to the campaign sender who mailed them. Narrowing to ``created_by`` alone would
hide contacts from accounts that legitimately hold them, which reads as data
loss. Widening to ``owner_ids`` alone would lose rows written before the array
existed.

⚠️ WHAT THIS CANNOT FIX, AND WHY SOME LOOKUPS STAY GLOBAL. While the unique
index stands, a path that looks a contact up **in order to create one if it is
missing** cannot be scoped: scoping the read makes it miss another account's
row, the insert then violates the index, and a silent cross-tenant attach
becomes a hard failure in a customer's import. ``app/ingestion/service.py``
says so at the point of the fallback — *"email is globally unique — fall back
to a global lookup so we never duplicate-key on insert"*.

So those paths keep a global read and are fixed on the WRITE side instead: find
the row globally, but do not modify a record this account does not own. See
``may_write``. The global reads are funnelled through
``ContactCrud.find_by_email_global``, which demands a ``reason``, so what
remains unscoped is auditable rather than invisible — and the list of reasons is
the work the fork (CRM-P2-000 proper) deletes.

⚠️ WHY IT LIVES IN ``api/services/`` AND NOT IN ``api/services/engage/``. That
package's ``__init__`` sets a rule — *"every module in this package imports
only the Python standard library. No FastAPI, no Beanie, no Motor, no
third-party packages"* — and this module needs ``PyObjectId``, which pulls in
bson and pydantic. Its `crm/` subpackage has long since stopped honouring that
rule, which is a reason not to add to the drift rather than a licence to. The
DB-touching services (``reply_ingestor``, ``segment_service``,
``suppression_overview``) live here, so this does too. Do not move it back.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Aliased so the module-level name cannot be shadowed by the many locals called
# `settings` in this service — `app/routes/settings.py` has one, and the CRUD
# dependency is named that too. `app.config` imports nothing from app.services,
# so this cannot cycle.
from app.config import settings as _settings
from app.object_id import PyObjectId

# The two keys that can carry the account on a contact query. Anything claiming
# to be scoped has to use one of them.
ACCOUNT_KEYS = ("created_by", "owner_ids")


class ContactScopeError(ValueError):
    """A contact query that should have been account-scoped and was not."""


def account_id(value: Any) -> Optional[PyObjectId]:
    """``value`` as an ObjectId, or None if it cannot be one.

    Accepts a ``str``, an ``ObjectId`` or a ``PyObjectId``, because the scope
    key arrives as all three in this codebase — ``ctx.account_id`` is a string,
    ``user.id`` is an ObjectId — and comparing the two spellings is one of the
    ways a scoped query silently matches nothing.
    """
    if value is None or value == "":
        return None
    try:
        return PyObjectId(str(value))
    except Exception:  # pylint: disable=broad-except
        return None


def account_clause(account: Any) -> Dict[str, Any]:
    """``{"$or": [{"owner_ids": id}, {"created_by": id}]}``.

    Raises rather than returning an empty dict for an unusable account: an
    empty clause merged into a filter is an UNSCOPED query that looks scoped,
    which is worse than a crash because it reaches production.
    """
    oid = account_id(account)
    if oid is None:
        raise ContactScopeError(
            f"cannot scope a contact query to account {account!r}")
    return {"$or": [{"owner_ids": oid}, {"created_by": oid}]}


def scoped(filters: Optional[Dict[str, Any]], account: Any) -> Dict[str, Any]:
    """``filters`` restricted to one account.

    ⚠️ COMBINED WITH ``$and``, NEVER BY SETTING ``$or``. Several of these
    filters already carry an ``$or`` — the ``q`` search over email, first name
    and last name is one — and assigning ``filters["$or"] = …`` would silently
    delete it, turning a search into "every contact in the account". Mongo
    allows only one ``$or`` per level, which is exactly why the existing call
    sites hand-roll an ``$and`` and why doing it in one place is worth it.
    """
    base = dict(filters or {})
    clause = account_clause(account)

    existing = base.pop("$and", None)
    clauses: List[Dict[str, Any]] = list(existing or [])
    if "$or" in base:
        clauses.append({"$or": base.pop("$or")})
    clauses.append(clause)

    if len(clauses) == 1:
        base.update(clauses[0])
    else:
        base["$and"] = clauses
    return base


def assert_account_scoped(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``filters`` unchanged, or raise if nothing in it names an account.

    The platform twin of ``crm/queries.assert_account_scoped``. Looks inside
    ``$and`` and ``$or`` branches, because the scope usually arrives as a
    branch rather than a top-level key.
    """
    if _names_account(filters):
        return filters
    raise ContactScopeError(
        f"contact query is not account-scoped: {sorted((filters or {}).keys())}")


def _names_account(node: Any) -> bool:
    if isinstance(node, dict):
        if any(key in node for key in ACCOUNT_KEYS):
            return True
        return any(_names_account(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(_names_account(item) for item in node)
    return False


def owns(contact: Any, account: Any) -> bool:
    """Does ``account`` hold this already-loaded contact?

    The post-fetch check, for the paths that legitimately fetch by ``_id`` —
    a tracking link, a DSAR export — where the id comes from outside and the
    query cannot be scoped. ``api/routes/contacts.py::_contact_owned_by`` is
    this check, written once in one file; this is the importable version.
    """
    oid = account_id(account)
    if contact is None or oid is None:
        return False
    if account_id(getattr(contact, "created_by", None)) == oid:
        return True
    return any(account_id(owner) == oid
               for owner in (getattr(contact, "owner_ids", None) or []))


def may_write(contact: Any, account: Any) -> bool:
    """May ``account`` modify this contact's own fields?

    ⚠️ THE SAME ANSWER AS ``owns``, AND DELIBERATELY A SEPARATE FUNCTION. While
    the unique index stands, several paths must read a contact globally in order
    not to duplicate-key on insert (see the module docstring), and the fix for
    those is not to change which row they find — it is to stop them writing to a
    row they do not own.

    Those call sites are asking a different question from "may I show this", and
    naming it separately is what makes the audit possible: every ``may_write``
    is a place where today's code wrote to another account's contact. When the
    fork lands, this function's callers are the list of writes to re-examine.

    What a caller does on False is its own decision and must be deliberate:
    attach the account to ``owner_ids`` and leave the fields alone (an import),
    or refuse the row and report it (a consent change). Silently writing anyway
    is the bug this exists to prevent.
    """
    return owns(contact, account)


# ── the contact-unification cutover ──────────────────────────────────────────

def unified(account_settings: Any) -> bool:
    """Does this account serve contacts from the unified path?

    Phase 3 of `docs/contact-unification-plan.md`, decision 0.1. **The only
    place either switch is read.** Every branch in phases 4–6 asks this
    function and nothing re-reads `settings.unified_contacts` or
    `crm_settings.unified_contacts` for itself.

    ⚠️ THAT SINGLE-READ RULE IS THE WHOLE DESIGN. Two paths exist for this
    object during the rollout, and the failure mode of a flag read in several
    places is not an outage — it is one request serving a contact from
    `crm_contacts` and the next serving it from `contacts`, for the same
    account, with the user watching fields appear and vanish. A second reader
    that disagrees by one `or` is indistinguishable from a caching bug.

    ⚠️ THE ENVIRONMENT WINS, AND IT WINS BOTH WAYS. `settings.unified_contacts`
    is tri-state: `None` hands the decision to the account, `False` is the kill
    switch that reverts everybody in a deploy rather than a write per account,
    and `True` is the end of the rollout. An account flag can therefore be
    overruled while still being *recorded*, so nothing is lost when the
    override is removed — which is what makes reverting safe to do in a hurry.

    Args:
        account_settings: this account's `CrmSettingsModel`, or None. None
            means settings could not be loaded, which is NOT a reason to
            switch paths: it answers False, the behaviour the account already
            had. A transient settings read failure must not move a user's
            contacts to a different collection for one request.
    """
    override = getattr(_settings, "unified_contacts", None)
    if override is not None:
        return bool(override)
    return bool(getattr(account_settings, "unified_contacts", False))


def unified_override() -> Optional[str]:
    """The environment override as a word, for `/health`: on, off, or None.

    None means no override is set and each account's own flag decides. Reported
    rather than inferred so "did the kill switch actually take?" is one curl
    instead of a deploy inspection.
    """
    override = getattr(_settings, "unified_contacts", None)
    if override is None:
        return None
    return "on" if override else "off"
