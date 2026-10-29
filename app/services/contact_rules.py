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


#: Fields that describe the PERSON and live on the platform `contacts` row.
#: The split rule, stated once: a fact about the person is here, a fact about
#: this account's relationship with them is on the profile.
PERSON_FIELDS = frozenset({
    "email", "first_name", "last_name", "phone", "job_title", "linkedin_url",
})

#: Fields that describe THIS ACCOUNT'S RELATIONSHIP and live on the profile.
#: `custom_fields` is here and not above on purpose: the platform contact has
#: a `custom_fields` of its own holding zero-party FORM data keyed by form
#: field name, and CRM attribute values are keyed by `internal_name`. Merging
#: the namespaces would let one account's attribute overwrite another
#: account's form answer — see the plan §2.
PROFILE_FIELDS = frozenset({
    "owner_id", "lifecycle_stage", "description", "source",
    "primary_company_id", "tag_ids", "custom_fields", "is_archived",
})


def split_updates(updates: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """One PATCH body, sorted into (person fields, profile fields).

    ⚠️ **A KEY IN NEITHER SET IS DROPPED, NOT GUESSED AT.** Routing an unknown
    field to the profile would silently write it into a document whose schema
    does not have it, where it would never be read back and never be reported
    — the quietest kind of data loss. Routing it to the person would write it
    to a collection this service does not own. The route validates its payload
    before calling this, so anything unrecognised here is a bug worth losing
    loudly rather than storing invisibly.
    """
    person = {k: v for k, v in (updates or {}).items() if k in PERSON_FIELDS}
    profile = {k: v for k, v in (updates or {}).items() if k in PROFILE_FIELDS}
    return person, profile


def new_person_fields(values: Dict[str, Any]) -> Dict[str, Any]:
    """The `contacts` document for somebody created from the CRM.

    ⚠️ **`subscribed` IS FORCED FALSE, AND THIS IS THE MOST IMPORTANT LINE IN
    THIS MODULE.** `Contact.subscribed` defaults to **True** on the model, so
    a create that merely omits it opts a sales lead into marketing — a consent
    decision made by forgetting, in a product whose whole compliance story is
    that CRM presence is not marketing permission. Adding somebody to a
    pipeline is not them asking to be emailed.

    ⚠️ AND NO `subscribe_date`. That field is the record of WHEN consent was
    given; stamping it here would forge one. Consent is granted through the
    marketing side, which sets both together.
    """
    fields = {k: v for k, v in (values or {}).items() if k in PERSON_FIELDS}
    fields["subscribed"] = False
    fields["origin"] = "crm"
    return fields


def unified_payload(contact: Any, profile: Any, *, owner: Any = None,
                    company: Optional[Dict[str, Any]] = None,
                    linked_deals: Optional[Dict[str, Any]] = None,
                    linked_companies: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """One contact as the API returns it, assembled from the PERSON and this
    account's PROFILE of them. Contact unification, phase 4.

    ⚠️ **THE SHAPE IS IDENTICAL TO ``payload()``, AND THAT IS THE REQUIREMENT,
    NOT A COINCIDENCE.** Both sides of the cutover flag serve the same
    `ContactDetails` to the same screen. A field that appears on one path and
    not the other means flipping an account's flag changes its API response —
    and the client was not redeployed, so the symptom is a blank column that
    nobody changed. When either builder gains a key, so must the other.

    ⚠️ **``id`` IS THE PLATFORM CONTACT'S ID, NOT THE PROFILE'S.** This is the
    one id space phase 6 converges on: associations, tasks and notes will all
    point at the person. Returning the profile's `_id` here would put a second
    id back into circulation through the front door, which is precisely what
    this whole change exists to remove. The profile's own id is never exposed.

    ⚠️ **NAME AND EMAIL COME FROM THE PERSON, NOT THE CACHE.** The profile
    carries `full_name` / `email` / `job_title` so the list can sort and search
    locally — but they are a cache, and a cache is not what you render when you
    are holding the authoritative record. Reading them from `contact` here is
    what keeps a stale cache a missed search hit rather than a wrong name on
    the screen.

    Args:
        contact: the platform `contacts` row (a dict or ForeignRecord).
        profile: this account's `crm_contact_profiles` row, or None for a
            marketing contact nobody here has worked yet. None yields the
            person with empty relationship fields — a real, renderable state.
    """
    out = {
        # ── the person ───────────────────────────────────────────────────────
        "id": _get(contact, "id") or _get(contact, "_id"),
        "email": _get(contact, "email"),
        "first_name": _get(contact, "first_name"),
        "last_name": _get(contact, "last_name"),
        "full_name": display_name(_get(contact, "first_name"),
                                  _get(contact, "last_name"),
                                  _get(contact, "email")),
        "phone": _get(contact, "phone"),
        "phone_country_code": _get(contact, "phone_country_code"),
        "job_title": _get(contact, "job_title"),
        "linkedin_url": _get(contact, "linkedin_url"),

        # ── this account's relationship with them ────────────────────────────
        "lifecycle_stage": _get(profile, "lifecycle_stage"),
        "description": _get(profile, "description"),
        "source": _get(profile, "source"),
        "owner": owner,
        "primary_company": company,
        "tag_ids": list(_get(profile, "tag_ids") or []),
        "custom_fields": dict(_get(profile, "custom_fields") or {}),
        "deal_count": int(_get(profile, "deal_count") or 0),
        "open_deal_value": float(_get(profile, "open_deal_value") or 0.0),
        "open_task_count": int(_get(profile, "open_task_count") or 0),
        "overdue_task_count": int(_get(profile, "overdue_task_count") or 0),
        "is_archived": bool(_get(profile, "is_archived")),
        "last_activity_at": _get(profile, "last_activity_at"),
        # Dates describe the RELATIONSHIP — when this account started working
        # the person, not when marketing first saw them. A contact subscribed
        # in 2023 and worked last week reads as a 2023 lead otherwise.
        "created_at": _get(profile, "created_at"),
        "updated_at": _get(profile, "updated_at"),

        # ⚠️ READ STRAIGHT OFF THE PERSON, AND NEVER CACHED ON THE PROFILE.
        # Consent is the one thing that must not be served stale: a rep
        # deciding whether to mail somebody needs today's answer, not the
        # reconcile job's. It costs nothing — the contact row is already here.
        "marketing": {
            "subscribed": bool(_get(contact, "subscribed")),
            "subscribe_date": _get(contact, "subscribe_date"),
            "list_count": len(_get(contact, "list_ids") or []),
        },
    }
    for key, value in (("linked_deals", linked_deals),
                       ("linked_companies", linked_companies)):
        if value is not None:
            out[key] = value
    return out


def payload(contact: Any, *, owner: Any = None,
            company: Optional[Dict[str, Any]] = None,
            linked_deals: Optional[Dict[str, Any]] = None,
            linked_companies: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One contact as the API returns it.

    `linked_deals` / `linked_companies` are only passed by the DETAIL route.
    The list route leaves them out, and the key is then absent from the dict
    rather than present as None — see the loop at the end.
    """
    out = {
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
        # ⚠️ ALWAYS None HERE, AND THE KEY IS STILL EMITTED. A legacy CRM
        # contact has no marketing record — that is what the severance meant —
        # and `None` says exactly that. Omitting the key instead would make
        # the two builders disagree on shape, which is the one thing they must
        # never do.
        "marketing": None,
    }
    # Omitted entirely when not supplied, so a list row serialises exactly as
    # it did before this existed and `ContactDetails` falls back to its own
    # default of None.
    for key, value in (("linked_deals", linked_deals),
                       ("linked_companies", linked_companies)):
        if value is not None:
            out[key] = value
    return out
