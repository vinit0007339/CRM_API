"""Pure rules for CRM contacts. E25 / CRM-P2-000.

⚠️ FOUR THINGS THIS MODULE EXISTS TO DECIDE.

1. **The dedupe key.** A CRM contact's email is unique PER ACCOUNT
   (`uniq_crm_contact_email`), and the key is the NORMALISED address. The
   normalisation and the stored value therefore have to be the same function,
   used on both the write and the duplicate check — otherwise the check passes,
   the insert hits the index, and a friendly "you already have this contact"
   reaches the user as a 500.

2. **That a contact may have no email.** A phone-only lead from a trade show is
   a real contact. The index is `sparse` for that reason, and nothing here may
   require an address — which also means the duplicate check has to be skipped
   rather than run against `None`.

3. **What "linked" means.** A CRM contact is marketable only once somebody adds
   it to a list, which is when `contact_id` is set. `None` is the NORMAL state,
   not a missing link, and nothing may read it as an error.

4. **The display name.** `full_name` is denormalised so the list can sort on it,
   so it is derived in exactly one place — and it falls back through the email
   to a placeholder, because a contact row with a blank name cell is unusable.

Everything here is a pure function over plain dicts, the way ``filter_rules``
and ``saved_view_rules`` are.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The lifecycle a contact moves through. Deliberately short: these are the
# stages a sales team actually acts on, and a longer list becomes a field
# nobody maintains. An account that wants its own vocabulary uses a custom
# single_select attribute instead.
LIFECYCLE = ("subscriber", "lead", "qualified", "customer", "churned")

# Roles a contact plays on a deal or a company (CRM-323). A contact can hold
# several on one deal — a champion who is also the economic buyer.
ROLES = ("decision_maker", "champion", "influencer", "blocker",
         "technical", "billing", "other")

NAME_MAX = 120
EMAIL_MAX = 254          # RFC 5321's limit on a forward path
UNNAMED = "Unnamed contact"

# Deliberately permissive: this rejects what is obviously not an address, and
# nothing more. A stricter pattern rejects real addresses (plus-tags, quoted
# locals, new TLDs) and the only authority on deliverability is a send.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def normalize_email(value: Any) -> Optional[str]:
    """The stored form of an address, and therefore the dedupe key.

    Lowercased and trimmed. ``None`` for anything empty — which is a valid
    contact, not an error (see the module docstring).

    ⚠️ DOTS AND PLUS-TAGS ARE **NOT** STRIPPED. Gmail treats
    ``p.riya+crm@gmail.com`` and ``priya@gmail.com`` as one mailbox; most other
    providers do not, and plenty of corporate systems issue
    ``first.last@company.com`` as distinct people. Canonicalising those would
    silently merge two different contacts — and the merge is unrecoverable
    because the second address was never stored. So the rule is: store what the
    user typed, lowercased.
    """
    text = str(value or "").strip().lower()
    return text or None


def valid_email(value: Any) -> bool:
    """Is this something we are willing to store as an address?"""
    text = normalize_email(value)
    if not text:
        return False
    if len(text) > EMAIL_MAX:
        return False
    return bool(_EMAIL.match(text))


def clean_email(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(normalised, error)``. An EMPTY address is allowed and returns
    ``(None, None)`` — only a malformed one is refused."""
    text = normalize_email(value)
    if text is None:
        return None, None
    if not valid_email(text):
        return None, f"“{value}” is not an email address."
    return text, None


def clean_name(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """A first or last name: whitespace collapsed, capped, optional."""
    name = " ".join(str(value or "").split())
    if not name:
        return None, None
    if len(name) > NAME_MAX:
        return None, f"A name is at most {NAME_MAX} characters."
    return name, None


def display_name(first: Any = None, last: Any = None,
                 email: Any = None) -> str:
    """The denormalised ``full_name``, derived in ONE place.

    ⚠️ IT NEVER COMES BACK EMPTY. `full_name` is what the list sorts and
    searches on; a blank cell is a row the user cannot identify and a sort key
    that groups every nameless contact together at one end. So: the name, else
    the address, else a placeholder.
    """
    parts = [p for p in (clean_name(first)[0], clean_name(last)[0]) if p]
    if parts:
        return " ".join(parts)
    address = normalize_email(email)
    return address or UNNAMED


def clean_lifecycle(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """A lifecycle stage, or an error. Empty is allowed — unset is a state."""
    text = str(value or "").strip().lower()
    if not text:
        return None, None
    if text not in LIFECYCLE:
        return None, (f"“{value}” is not a lifecycle stage. "
                      f"Choose one of: {', '.join(LIFECYCLE)}.")
    return text, None


def clean_role(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """A role on a deal or company. Unset means "linked, role unstated"."""
    text = str(value or "").strip().lower()
    if not text:
        return None, None
    if text not in ROLES:
        return None, (f"“{value}” is not a contact role. "
                      f"Choose one of: {', '.join(ROLES)}.")
    return text, None


def email_domain(value: Any) -> Optional[str]:
    """The domain part of an address, lowercased — the auto-link key."""
    text = normalize_email(value)
    if not text or "@" not in text:
        return None
    domain = text.rsplit("@", 1)[-1].strip()
    return domain or None


def duplicate_message(existing: Any) -> str:
    """What to say when the account already holds this address.

    Names the contact rather than the rule, and says where it is, because
    "duplicate email" leaves the user with nothing to click.
    """
    name = _get(existing, "full_name") or _get(existing, "email") or "a contact"
    return f"“{name}” already has that email address in this account."


def may_write(contact: Any, *, actor_user_id: Any,
              is_account_owner: bool = False,
              has_manage_others: bool = False) -> bool:
    """Ownership override, matching every other CRM record.

    Read is account-wide; WRITING somebody else's contact needs
    ``crm:manage_others`` or account ownership. A contact with NO owner is
    writable by anyone who can write contacts at all — an unowned record that
    nobody can edit is a dead row.
    """
    owner = _get(contact, "owner_id")
    if owner is None:
        return True
    if str(owner) == str(actor_user_id):
        return True
    return bool(is_account_owner or has_manage_others)


def write_refusal() -> str:
    return ("That contact belongs to someone else — you need the "
            "“manage others’ records” permission to change it.")


def apply_identity(values: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise the identity fields of a create/update payload.

    Returns only the keys it actually touched, so a PATCH stays a PATCH: a
    caller that did not send `last_name` must not have it written as None.
    `full_name` is recomputed whenever ANY of its inputs moved.
    """
    out: Dict[str, Any] = {}
    for field in ("first_name", "last_name", "job_title"):
        if field in values:
            out[field] = clean_name(values.get(field))[0]
    if "email" in values:
        out["email"] = normalize_email(values.get("email"))
    return out


def validate(values: Dict[str, Any]) -> Optional[str]:
    """The first thing wrong with a create/update payload, or None.

    ⚠️ NO `if "x" in values` GUARDS HERE, ON PURPOSE. Every cleaner above
    treats an EMPTY value as allowed, and an absent key reads as empty — so a
    guard would change nothing and would imply this function distinguishes
    "not sent" from "sent blank" when it does not. `apply_identity` is where
    that distinction is real and is made.
    """
    for cleaner, field in ((clean_email, "email"),
                           (clean_name, "first_name"),
                           (clean_name, "last_name"),
                           (clean_name, "job_title"),
                           (clean_lifecycle, "lifecycle_stage")):
        _, error = cleaner(values.get(field))
        if error:
            return error
    return None


def has_identity(values: Dict[str, Any]) -> bool:
    """Is there enough here to be a contact at all?

    ⚠️ A CONTACT NEEDS A NAME OR AN ADDRESS. Neither, and the row renders as
    "Unnamed contact" with nothing to search for or write to — a record that
    cannot be found again, which is worse than a refused create.
    """
    if normalize_email(values.get("email")):
        return True
    return bool(clean_name(values.get("first_name"))[0]
                or clean_name(values.get("last_name"))[0])


def payload(contact: Any, *, owner: Any = None,
            company: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One contact as the API returns it."""
    return {
        "id": _get(contact, "id"),
        "email": _get(contact, "email"),
        "first_name": _get(contact, "first_name"),
        "last_name": _get(contact, "last_name"),
        "full_name": _get(contact, "full_name") or display_name(
            _get(contact, "first_name"), _get(contact, "last_name"),
            _get(contact, "email")),
        "phone": _get(contact, "phone"),
        "phone_country_code": _get(contact, "phone_country_code"),
        "job_title": _get(contact, "job_title"),
        "linkedin_url": _get(contact, "linkedin_url"),
        "lifecycle_stage": _get(contact, "lifecycle_stage"),
        "description": _get(contact, "description"),
        "source": _get(contact, "source"),
        "owner": owner,
        "primary_company": company,
        "tag_ids": list(_get(contact, "tag_ids") or []),
        "custom_fields": dict(_get(contact, "custom_fields") or {}),
        "deal_count": int(_get(contact, "deal_count") or 0),
        "open_deal_value": float(_get(contact, "open_deal_value") or 0.0),
        "open_task_count": int(_get(contact, "open_task_count") or 0),
        "overdue_task_count": int(_get(contact, "overdue_task_count") or 0),
        "is_archived": bool(_get(contact, "is_archived")),
        "last_activity_at": _get(contact, "last_activity_at"),
        "created_at": _get(contact, "created_at"),
        "updated_at": _get(contact, "updated_at"),
    }
