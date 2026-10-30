"""One place that splits an attribute payload into typed columns and custom
fields. CRM-208.

Extracted because there were three callers and two copies: ``companies.py`` and
``deals.py`` each had their own ``_route_attribute_values``, and the import
worker would have made a third. Two copies of a validation rule is how records
that arrive by import end up behaving differently from records that were typed
in — and the imported population is always the larger one.

Returns ``(typed, custom, error_message)``. The error is a message rather than
an exception because every caller wants to turn it into a business failure,
not a traceback.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from app.services import attributes as attr_rules


# The model's own columns, per object type. Used ONLY when an account has no
# attribute rows yet — see the fallback in `route_values`.
SYSTEM_FIELDS = {
    "company": {
        "name", "domain", "website", "linkedin_url", "industry",
        "employee_count", "annual_revenue", "currency", "phone",
        "phone_country_code", "description",
    },
    # ⚠️ `currency` IS ABSENT ON PURPOSE. It is the PIPELINE's, not the lead's
    # (see `serializers.DEAL_UPDATABLE`). Listed here it would route a
    # `custom_fields: {"currency": "EUR"}` straight onto the column and
    # reintroduce a mixed-currency pipeline through the back door.
    "deal": {"name", "amount", "expected_close_date", "description"},
    # E25, restored with the contacts object. These are TYPED COLUMNS on
    # CrmContactModel, so a custom value under one of these names must route to
    # the column and not into `custom_fields` — which is the whole job of this
    # map. `full_name` is absent on purpose: it is derived by
    # `contact_rules.display_name` and no account may write it.
    "contact": {
        "email", "first_name", "last_name", "phone", "phone_country_code",
        "job_title", "linkedin_url", "lifecycle_stage", "description",
    },
}

# How each TYPED COLUMN's value must be coerced on the no-definitions path.
#
# ⚠️ PYDANTIC DOES NOT VALIDATE ON ASSIGNMENT, so whatever lands here is
# written to the document as-is and only fails when the record is READ BACK.
# That is not a theoretical risk: a deal was stored with
#
#     expected_close_date = "cylonsign.com"
#
# and from then on `GET /crm/deals?pipeline_id=…` answered 500 for the whole
# pipeline — one bad row took out everybody's board, with a pydantic traceback
# instead of a message. It got there because this map covered NUMBERS ONLY and
# the date column had no coercion at all.
#
# ⚠️ AND AN UNCOERCIBLE VALUE IS NOW AN ERROR, where it used to be passed
# through. The old comment reasoned that this is "the unvalidated path by
# definition" and that refusing would reject a record a definition-less
# account could save yesterday. That was wrong in one specific way: a record
# whose typed column cannot be read back was never saved, it was corrupted —
# the write returned 200 and every later read of it failed. Refusing the write
# is the lenient option, because it is the one the user can act on.
_NUMERIC_SYSTEM_FIELDS = {"employee_count": int, "annual_revenue": float,
                          "amount": float}

# Columns typed `Optional[datetime]` on the model. Kept beside the numerics so
# a third kind of column cannot be added to SYSTEM_FIELDS without this file
# saying how to coerce it — see `test_every_system_field_has_a_known_type`.
_DATE_SYSTEM_FIELDS = {"expected_close_date"}


class _Uncoercible(ValueError):
    """A typed column that cannot take this value. Carries a user sentence."""


def _label(field: str) -> str:
    return field.replace("_", " ").capitalize()


def _coerce(field: str, value: Any) -> Any:
    """The value as its column's type, or ``_Uncoercible`` with a sentence."""
    if value is None or value == "":
        return value
    caster = _NUMERIC_SYSTEM_FIELDS.get(field)
    if caster is not None:
        try:
            return caster(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            raise _Uncoercible(
                f"“{_label(field)}” needs a number.") from None
    if field in _DATE_SYSTEM_FIELDS:
        try:
            # The ONE date parser — the definitions path uses the same call,
            # so the two cannot disagree about what a date is.
            return attr_rules.coerce_datetime(value, _label(field))
        except attr_rules.AttributeError_ as ex:
            raise _Uncoercible(str(ex)) from None
    return value


def route_values(values: Optional[Dict[str, Any]],
                 attributes: Sequence[Any],
                 object_type: Optional[str] = None
                 ) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]:
    """Split and coerce. ``attributes`` is this account's definitions."""
    if not values:
        return {}, {}, None
    if not attributes:
        # CRM-208's backward-compatibility path: an account with no attribute
        # definitions yet accepts custom_fields unvalidated, so upgrading does
        # not reject data that worked yesterday. It closes once
        # `seed_crm_defaults.py --attributes-only` has run for the account.
        #
        # ⚠️ BUT THE SYSTEM COLUMNS STILL HAVE TO REACH THEIR TYPED FIELDS.
        # Without this, a company created from the form kept `industry`,
        # `domain` and `employee_count` inside custom_fields: no screen read
        # them, no filter found them, and the list showed an em-dash — while
        # the SAME record created by import got them right, because E16's
        # worker already recovered them from a static set. That split is
        # precisely what this module exists to prevent.
        system = SYSTEM_FIELDS.get(object_type or "", set())
        typed: Dict[str, Any] = {}
        for key, value in values.items():
            if key not in system:
                continue
            try:
                typed[key] = _coerce(key, value)
            except _Uncoercible as ex:
                # Returned as a message, like every other failure here: the
                # caller turns it into a business failure rather than a 500.
                return {}, {}, str(ex)
        custom = {k: v for k, v in values.items() if k not in system}
        return typed, custom, None
    try:
        typed, custom = attr_rules.split_values(values, attributes)
    except attr_rules.AttributeError_ as ex:
        return {}, {}, str(ex)
    return typed, custom, None


async def route_for(attributes_crud, account_id: Any, object_type: str,
                    values: Optional[Dict[str, Any]]
                    ) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[str]]:
    """``route_values`` with the definitions fetched for you.

    The route handlers use this; the import worker uses ``route_values``
    directly with a list it caches, because fetching per row would be one query
    per row for nine thousand rows.
    """
    if not values:
        return {}, {}, None
    rows = await attributes_crud.list(account_id=account_id,
                                      object_type=object_type)
    return route_values(values, rows, object_type)
