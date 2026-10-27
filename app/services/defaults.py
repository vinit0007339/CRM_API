"""Seeded CRM defaults. CRM-005.

One source of truth for the starter data, used by BOTH:

* ``GET /crm/pipelines``, which auto-provisions a default pipeline the first
  time an account opens the CRM (so nobody ever sees an empty board), and
* ``scripts/seed_crm_defaults.py``, the idempotent backfill.

Pure data + factories, no DB — so the shape of the default pipeline is
unit-tested rather than discovered in production.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

DEFAULT_PIPELINE_NAME = "Sales Pipeline"

# Adapted from Brevo's default (New / Qualifying / Demo scheduled / Pending
# commitment / In negotiation / Won / Lost). "Pending commitment" is renamed to
# "Proposal sent", which is what the stage actually means to a seller, and it
# carries the required-attribute rule so a deal can't reach a proposal without
# an amount and a close date.
DEFAULT_STAGES: List[Dict[str, Any]] = [
    {"name": "New",             "type": "open", "win_probability": 10,
     "color": "#8fa6b0", "rotting_days": 14, "required_attributes": []},
    {"name": "Qualifying",      "type": "open", "win_probability": 25,
     "color": "#4a90d9", "rotting_days": 14, "required_attributes": []},
    {"name": "Demo scheduled",  "type": "open", "win_probability": 40,
     "color": "#7e6bc4", "rotting_days": 10, "required_attributes": []},
    {"name": "Proposal sent",   "type": "open", "win_probability": 60,
     "color": "#e0803d", "rotting_days": 10,
     "required_attributes": ["amount", "expected_close_date"]},
    {"name": "In negotiation",  "type": "open", "win_probability": 80,
     "color": "#d4a017", "rotting_days": 7,  "required_attributes": []},
    {"name": "Won",             "type": "won",  "win_probability": 100,
     "color": "#00C49A", "rotting_days": None, "required_attributes": []},
    {"name": "Lost",            "type": "lost", "win_probability": 0,
     "color": "#FF5252", "rotting_days": None, "required_attributes": []},
]

DEFAULT_LOST_REASONS = [
    "Price too high",
    "Chose a competitor",
    "No budget",
    "No decision / went silent",
    "Bad timing",
    "Not a fit",
    "Duplicate",
]

DEFAULT_TASK_TYPES = ["todo", "call", "email", "meeting", "deadline", "linkedin", "lunch"]

DEFAULT_INDUSTRIES = [
    "Advertising & Marketing", "Agriculture", "Automotive", "Banking & Finance",
    "Construction", "Consulting", "Education", "Energy & Utilities",
    "Entertainment & Media", "Fashion & Apparel", "Food & Beverage", "Government",
    "Healthcare", "Hospitality & Travel", "Insurance", "Legal",
    "Logistics & Transport", "Manufacturing", "Non-profit", "Pharmaceuticals",
    "Real Estate", "Retail & E-commerce", "SaaS & Software",
    "Telecommunications", "Other",
]

# Contacts on these domains are never auto-linked to a company by email domain —
# otherwise every Gmail user in the account collapses into one "gmail.com"
# company. Extend per account via crm_settings.free_email_domains_blocklist.
FREE_EMAIL_DOMAINS = [
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk",
    "yahoo.fr", "yahoo.de", "ymail.com", "rocketmail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "tutanota.com", "hushmail.com",
    "zoho.com", "gmx.com", "gmx.de", "gmx.net", "mail.com", "email.com",
    "yandex.com", "yandex.ru", "mail.ru", "inbox.com", "fastmail.com",
    "rediffmail.com", "sify.com", "indiatimes.com",
    "qq.com", "163.com", "126.com", "sina.com", "naver.com", "daum.net",
    "web.de", "t-online.de", "orange.fr", "free.fr", "laposte.net",
    "libero.it", "virgilio.it", "terra.com.br", "uol.com.br", "bol.com.br",
]

# Core typed columns exposed through the attribute system so an account can
# reorder them on the creation form or mark one required — without a code
# change. is_system rows can be relabelled and reordered, never deleted or
# retyped. Writes to these route to the model column, not custom_fields.
SYSTEM_COMPANY_ATTRIBUTES = [
    ("name",           "Company name",   "text",          True),
    ("domain",         "Domain",         "url",           False),
    ("website",        "Website",        "url",           False),
    ("linkedin_url",   "LinkedIn",       "url",           False),
    ("industry",       "Industry",       "single_select", False),
    ("employee_count", "Employees",      "number",        False),
    ("annual_revenue", "Annual revenue", "currency",      False),
    ("phone",          "Phone",          "phone",         False),
    ("owner_id",       "Owner",          "user",          False),
    ("description",    "Description",    "textarea",      False),
]

SYSTEM_DEAL_ATTRIBUTES = [
    ("name",                "Deal name",           "text",     True),
    ("amount",              "Amount",              "currency", False),
    ("expected_close_date", "Expected close date", "date",     False),
    ("owner_id",            "Owner",               "user",      False),
    ("probability",         "Probability",         "number",   False),
    ("source",              "Source",              "text",     False),
    ("description",         "Description",         "textarea", False),
]

# ⚠️ `SYSTEM_CONTACT_ATTRIBUTES` WAS HERE AND IS GONE, with the CRM's people
# object. There is no `contact` object type any more: the app has one contacts
# list, the marketing audience, and it has its own `custom_fields` — it does
# not read this catalogue.
#
# Accounts that already defined contact attributes keep those rows; nothing
# deletes them. `scripts/migrate_crm_contacts_removal.py` reports them.

# E25, restored with the contacts object.
#
# ⚠️ NEITHER `email` NOR A NAME IS `is_required`. A phone-only lead from a
# trade show is a real contact, and `contact_rules.has_identity` enforces the
# real rule — a name OR an address — which no single required field can
# express. Marking email required here would block that lead at the form and
# at import; marking first_name required would block `support@acme.com`.
SYSTEM_CONTACT_ATTRIBUTES = [
    ("first_name",      "First name",      "text",          False),
    ("last_name",       "Last name",       "text",          False),
    ("email",           "Email",           "email",         False),
    ("phone",           "Phone",           "phone",         False),
    ("job_title",       "Job title",       "text",          False),
    ("lifecycle_stage", "Lifecycle stage", "single_select", False),
    ("linkedin_url",    "LinkedIn",        "url",           False),
    ("owner_id",        "Owner",           "user",          False),
    ("description",     "Description",     "textarea",      False),
]


SYSTEM_ATTRIBUTES_BY_OBJECT = {
    "company": SYSTEM_COMPANY_ATTRIBUTES,
    "deal": SYSTEM_DEAL_ATTRIBUTES,
    "contact": SYSTEM_CONTACT_ATTRIBUTES,
}

# ⚠️ EVERY caller that iterates object types must derive them from HERE.
# `contact` was added to SYSTEM_ATTRIBUTES_BY_OBJECT in E25 while
# `seed_crm_defaults.py` kept its own hard-coded ("company", "deal") tuple, so
# the backfill silently skipped contacts for a whole phase. It has since been
# removed again — but the lesson stands for the next object type, and the
# reason this tuple is derived rather than written out is that incident.
SYSTEM_OBJECT_TYPES: Tuple[str, ...] = tuple(SYSTEM_ATTRIBUTES_BY_OBJECT)


def build_default_stages() -> List[Dict[str, Any]]:
    """Fresh copy of the default stage set, with ``order`` filled in.

    Returns dicts (not PipelineStage instances) so this module stays DB-free;
    the caller constructs the model. Deep-copied per call — a shared mutable
    default would leak edits from one account into the next.
    """
    return [
        {**stage, "order": i} for i, stage in enumerate(DEFAULT_STAGES)
    ]


def build_default_lost_reasons() -> List[Dict[str, Any]]:
    return [
        {"label": label, "order": i, "is_active": True}
        for i, label in enumerate(DEFAULT_LOST_REASONS)
    ]


def build_system_attributes(object_type: str) -> List[Dict[str, Any]]:
    """Rows for ``crm_attributes`` describing the core typed columns."""
    # ⚠️ A LOOKUP, NOT AN if/else. The old spelling handed EVERY unknown
    # object type the deal attributes, so a typo seeded a company with
    # "Expected close date" and nothing failed.
    source = SYSTEM_ATTRIBUTES_BY_OBJECT.get(object_type)
    if source is None:
        return []
    return [
        {
            "object_type": object_type,
            "internal_name": internal,
            "label": label,
            "attribute_type": attr_type,
            "is_required": required,
            "is_system": True,
            "show_on_form": True,
            "form_order": i,
        }
        for i, (internal, label, attr_type, required) in enumerate(source)
    ]


def system_attribute_count() -> int:
    """How many ``is_system`` rows a fully provisioned account should hold.

    The cheap guard on the read path: one count against this number decides
    whether a diff is worth doing at all. It must stay derived from
    ``SYSTEM_ATTRIBUTES_BY_OBJECT`` — a literal here would go stale the moment
    an attribute is added, and the drift would be invisible.
    """
    return sum(len(rows) for rows in SYSTEM_ATTRIBUTES_BY_OBJECT.values())


def missing_system_attributes(
    existing: Iterable[Tuple[str, str]]
) -> List[Dict[str, Any]]:
    """Rows for ``crm_attributes`` that this account is missing. CRM-P0-002.

    ``existing`` is ``(object_type, internal_name)`` for the account's current
    ``is_system`` rows. Returns insertable rows — each already carrying its
    ``object_type`` — for every system attribute not in that set, across every
    object type in ``SYSTEM_ATTRIBUTES_BY_OBJECT``.

    Three things this deliberately does:

    * **Compares per name, never per object type.** "Does this account have any
      contact attributes?" is the wrong question: an account part-way through a
      phase boundary has some and not others. Asking per name means a phase that
      adds one field backfills exactly that field.
    * **Never touches a custom attribute.** Only ``is_system`` names are
      considered, so a user's own attributes cannot be clobbered or re-ordered.
    * **Preserves ``form_order``** from the catalogue, so a backfilled field
      lands where the form expects it rather than at the end.
    """
    # ⚠️ `object_type` is a CrmObjectType on the model, and `str(SomeEnum.DEAL)`
    # is "CrmObjectType.DEAL", not "deal" — so a plain str() here matches
    # nothing and the provisioner duplicates the entire catalogue on every
    # read. The house rule is getattr(x, "value", x), and this function was
    # written wrong once already.
    have: Set[Tuple[str, str]] = {
        (str(getattr(obj, "value", obj)), str(getattr(name, "value", name)))
        for obj, name in existing
    }
    out: List[Dict[str, Any]] = []
    for object_type in SYSTEM_OBJECT_TYPES:
        for row in build_system_attributes(object_type):
            if (object_type, row["internal_name"]) in have:
                continue
            out.append(row)
    return out


def default_settings_payload() -> Dict[str, Any]:
    """Everything a fresh ``crm_settings`` document should start with."""
    return {
        "currency": "INR",
        "industries": list(DEFAULT_INDUSTRIES),
        "lost_reasons": build_default_lost_reasons(),
        "task_types": list(DEFAULT_TASK_TYPES),
        "auto_link_contacts_by_domain": True,
        "free_email_domains_blocklist": list(FREE_EMAIL_DOMAINS),
        "card_display_attributes": ["amount", "expected_close_date"],
    }


def is_free_email_domain(domain: str, blocklist: List[str] = None) -> bool:
    """Should a contact on this domain be kept out of domain auto-linking?"""
    if not domain:
        return True
    return str(domain).strip().lower() in set(blocklist or FREE_EMAIL_DOMAINS)
