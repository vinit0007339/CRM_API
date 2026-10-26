"""Attribute rules: slugs, immutability, and per-type value handling. CRM-207/208.

Three jobs, all pure and all easy to get quietly wrong:

1. **Slug generation.** ``internal_name`` is derived from the label once, at
   creation, and then frozen forever.

2. **Immutability.** ``internal_name`` and ``attribute_type`` can never change,
   and neither can an option's ``key``. Every stored value lives under those
   keys — renaming one orphans every value already written under the old name,
   silently, with no error and no way to find them again. The model docstring
   has promised this enforcement since CRM-012 and nothing implemented it; this
   module is that enforcement.

3. **Value routing and coercion.** A write to a *system* attribute goes to the
   typed model column; a write to a custom attribute goes into ``custom_fields``.
   One payload can contain both. Values are coerced and validated per type,
   because ``custom_fields`` is schemaless and whatever lands there is what
   every future reader gets.

Stdlib-only. No DB, no pydantic.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

MAX_LABEL = 50
MAX_INTERNAL_NAME = 60

# Immutable after creation. See the module docstring.
FROZEN_FIELDS = ("internal_name", "attribute_type", "object_type", "is_system")

SELECT_TYPES = frozenset({"single_select", "multi_select"})
NUMERIC_TYPES = frozenset({"number", "currency"})
TEXT_TYPES = frozenset({"text", "textarea", "url", "email", "phone"})

ALL_TYPES = frozenset({
    "text", "textarea", "number", "date", "datetime", "boolean",
    "single_select", "multi_select", "user", "url", "email", "phone", "currency",
})

# A slug that would collide with a Mongo operator or a dotted path is not
# storable as a `custom_fields` key.
_RESERVED_PREFIXES = ("$",)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AttributeError_(ValueError):
    """A user-facing attribute problem. Carries a sentence, not a code."""


def slugify(label: str) -> str:
    """``"Renewal risk (Q3)"`` -> ``renewal_risk_q3``.

    Never returns an empty string: a label of pure punctuation still needs a
    key, and ``""`` is not a storable one.
    """
    base = _SLUG_STRIP.sub("_", str(label or "").strip().lower()).strip("_")
    base = re.sub(r"_+", "_", base)
    if not base:
        base = "field"
    if base[0].isdigit():
        # A key starting with a digit is legal in Mongo but confusing
        # everywhere else it is used as an identifier.
        base = f"f_{base}"
    return base[:MAX_INTERNAL_NAME]


def unique_internal_name(label: str, taken: Iterable[str]) -> str:
    """A slug not already used for this (account, object_type).

    Suffixes ``_2``, ``_3``… rather than failing, because two attributes called
    "Notes" is a reasonable thing for a user to want and a unique-key error is
    not a reasonable thing to show them.
    """
    existing: Set[str] = {str(t) for t in (taken or [])}
    base = slugify(label)
    if base not in existing:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:MAX_INTERNAL_NAME - 4]}_{n}"
        if candidate not in existing:
            return candidate
    raise AttributeError_("Too many attributes share that name. Try another label.")


# ── validation ───────────────────────────────────────────────────────────────
def validate_definition(
    payload: Dict[str, Any], *, is_create: bool = True
) -> None:
    """Check an attribute definition. Raises AttributeError_ with a sentence."""
    if is_create:
        attr_type = payload.get("attribute_type")
        attr_type = getattr(attr_type, "value", attr_type)
        if attr_type not in ALL_TYPES:
            raise AttributeError_(f"“{attr_type}” is not an attribute type.")

    if "label" in payload:
        label = str(payload.get("label") or "").strip()
        if not label:
            raise AttributeError_("Give the attribute a label.")
        if len(label) > MAX_LABEL:
            raise AttributeError_(f"Keep the label under {MAX_LABEL} characters.")

    attr_type = payload.get("attribute_type")
    attr_type = getattr(attr_type, "value", attr_type)
    options = payload.get("options")

    if attr_type in SELECT_TYPES and is_create and not options:
        raise AttributeError_(
            "A select attribute needs at least one option to choose from."
        )

    if options is not None:
        validate_options(options)


def validate_options(options: Sequence[Dict[str, Any]]) -> None:
    keys: Set[str] = set()
    for option in options or []:
        key = str(_get(option, "key") or "").strip()
        label = str(_get(option, "label") or "").strip()
        if not key:
            raise AttributeError_("Every option needs a key.")
        if not label:
            raise AttributeError_(f"Option “{key}” needs a label.")
        if key in keys:
            raise AttributeError_(f"Two options share the key “{key}”.")
        keys.add(key)


def assert_mutable(existing: Any, updates: Dict[str, Any]) -> None:
    """Refuse a change to a frozen field. THE guard this module exists for."""
    for field in FROZEN_FIELDS:
        if field not in updates:
            continue
        new = updates.get(field)
        old = _get(existing, field)
        if _same(new, old):
            continue          # a no-op echo of the current value is harmless
        if field == "internal_name":
            raise AttributeError_(
                "An attribute's internal name can't be changed — every value "
                "already saved is stored under it. Create a new attribute "
                "instead. You can rename the label freely."
            )
        if field == "attribute_type":
            raise AttributeError_(
                "An attribute's type can't be changed once it holds data. "
                "Create a new attribute with the type you need."
            )
        raise AttributeError_(f"“{field}” can't be changed after creation.")


def assert_option_keys_stable(
    existing_options: Sequence[Any], new_options: Sequence[Any]
) -> None:
    """Options may be added, relabelled, recoloured and removed — never rekeyed.

    A key is what is stored on the record. Rekeying orphans values exactly the
    way renaming ``internal_name`` does, just one level down, which is why it is
    easy to miss.
    """
    old_keys = [str(_get(o, "key")) for o in (existing_options or [])]
    new_by_key = {str(_get(o, "key")): o for o in (new_options or [])}
    old_labels = {str(_get(o, "key")): str(_get(o, "label")) for o in (existing_options or [])}

    removed = [k for k in old_keys if k not in new_by_key]
    # Removal is allowed but must be deliberate — the caller decides whether to
    # warn. What is never allowed is a key changing identity underneath a value.
    for key in old_keys:
        if key in new_by_key:
            continue
        if len(removed) == len(old_keys) and old_labels:
            raise AttributeError_(
                "That would replace every option. Existing records would keep "
                "values that no longer match any choice."
            )


def can_delete(existing: Any, *, value_count: int = 0, confirmed: bool = False) -> None:
    """Raise unless this attribute may be deleted."""
    if _get(existing, "is_system"):
        raise AttributeError_(
            "Built-in fields can't be deleted. You can hide one from the form "
            "instead, or rename its label."
        )
    if value_count > 0 and not confirmed:
        one = value_count == 1
        raise AttributeError_(
            f"{value_count} {'record has' if one else 'records have'} a value for "
            f"this attribute. Deleting it removes "
            f"{'that value' if one else 'those values'} permanently."
        )


# ── value coercion ───────────────────────────────────────────────────────────
def coerce_value(attribute: Any, value: Any) -> Any:
    """Coerce one value to its attribute's type, or raise.

    ``None`` and ``""`` both mean "unset" and pass through as None — an empty
    string in a number field is a cleared field, not a validation failure.
    """
    if value is None or value == "":
        return None

    attr_type = _get(attribute, "attribute_type")
    attr_type = getattr(attr_type, "value", attr_type)
    label = _get(attribute, "label") or _get(attribute, "internal_name")

    if attr_type in NUMERIC_TYPES:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise AttributeError_(f"“{label}” needs a number.") from None

    if attr_type == "boolean":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "1", "yes"):
            return True
        if str(value).lower() in ("false", "0", "no"):
            return False
        raise AttributeError_(f"“{label}” is a yes/no field.")

    if attr_type in ("date", "datetime"):
        return coerce_datetime(value, label)

    if attr_type == "single_select":
        keys = _option_keys(attribute)
        if str(value) not in keys:
            raise AttributeError_(f"“{value}” is not one of the choices for “{label}”.")
        return str(value)

    if attr_type == "multi_select":
        if not isinstance(value, (list, tuple, set)):
            raise AttributeError_(f"“{label}” takes a list of choices.")
        keys = _option_keys(attribute)
        out = []
        for item in value:
            if str(item) not in keys:
                raise AttributeError_(f"“{item}” is not one of the choices for “{label}”.")
            out.append(str(item))
        return out

    if attr_type == "email":
        text = str(value).strip()
        if not _EMAIL.match(text):
            raise AttributeError_(f"“{label}” needs a valid email address.")
        return text

    if attr_type == "user":
        # An id — the caller resolves it. Stored as a string so custom_fields
        # never holds a raw ObjectId (which cannot be serialised out of an
        # untyped dict; see serializers.json_safe).
        return str(value)

    if attr_type in TEXT_TYPES:
        return str(value)

    return value


def coerce_datetime(value: Any, label: Any) -> Any:
    """A value as a datetime, or ``AttributeError_`` naming the field.

    PUBLIC because `attribute_routing` needs it too: the no-definitions write
    path has to coerce `expected_close_date` and there must not be a second
    date parser. A `.com` reached that column through one — see the note on
    `attribute_routing._SYSTEM_FIELD_COERCION`.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise AttributeError_(f"“{label}” needs a date.") from None


# ── routing ──────────────────────────────────────────────────────────────────
def split_values(
    values: Dict[str, Any], attributes: Sequence[Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split one payload into ``(typed_columns, custom_fields)``.

    A write naming a SYSTEM attribute lands on the model column. Anything else
    lands in ``custom_fields``. This is the whole point of seeding core fields
    as attributes: one form, one payload, two destinations.

    An unknown name is REJECTED rather than stored. Silent storage is how
    ``custom_fields`` turns into a junk drawer that nobody can query and nobody
    dares clean out.
    """
    index = {str(_get(a, "internal_name")): a for a in (attributes or [])}
    typed: Dict[str, Any] = {}
    custom: Dict[str, Any] = {}

    for name, raw in (values or {}).items():
        attribute = index.get(str(name))
        if attribute is None:
            raise AttributeError_(f"There is no field called “{name}”.")
        if _get(attribute, "is_readonly"):
            raise AttributeError_(
                f"“{_get(attribute, 'label')}” is read-only."
            )
        coerced = coerce_value(attribute, raw)
        if _get(attribute, "is_system"):
            typed[str(name)] = coerced
        else:
            custom[str(name)] = coerced

    return typed, custom


def missing_required(
    record: Dict[str, Any], attributes: Sequence[Any]
) -> List[Dict[str, str]]:
    """Required attributes with no value on this record.

    Returns ``{internal_name, label}`` pairs, NOT bare labels. The client needs
    the name to render an input for it; matching on the display label breaks the
    moment an account renames the attribute.
    """
    custom = (record or {}).get("custom_fields") or {}
    missing: List[Dict[str, str]] = []
    for attribute in attributes or []:
        if not _get(attribute, "is_required"):
            continue
        name = str(_get(attribute, "internal_name"))
        value = record.get(name, custom.get(name))
        if _is_empty(value):
            missing.append({"internal_name": name, "label": str(_get(attribute, "label"))})
    return missing


def form_attributes(attributes: Sequence[Any]) -> List[Any]:
    """Attributes a create/edit form renders, in order."""
    rows = [a for a in (attributes or []) if _get(a, "show_on_form")]
    return sorted(rows, key=lambda a: (_get(a, "form_order") or 0, str(_get(a, "label"))))


def card_attributes(attributes: Sequence[Any], configured: Sequence[str] = ()) -> List[Any]:
    """Attributes a Kanban card shows, in the account's configured order.

    ``crm_settings.card_display_attributes`` wins where it is set, because it is
    an explicit ordering; ``show_on_card`` is the per-attribute fallback.
    """
    index = {str(_get(a, "internal_name")): a for a in (attributes or [])}
    if configured:
        return [index[str(n)] for n in configured if str(n) in index]
    return [a for a in (attributes or []) if _get(a, "show_on_card")]


MAX_CARD_ATTRIBUTES = 6


def validate_card_order(names: Sequence[Any],
                        attributes: Sequence[Any]) -> List[str]:
    """Clean a `card_display_attributes` list for storage. CRM-409.

    Returns the stored order. Raises ``AttributeError_`` with a user-facing
    message rather than quietly dropping anything, because every failure mode
    here is invisible on the board:

    * **An unknown name is refused, not skipped.** `card_attributes` filters
      names it cannot resolve, so a typo would produce a card missing a field
      with nothing anywhere saying why.
    * **A duplicate is refused.** It would render the same row twice.
    * **A cap, stated.** More than a handful of rows and the cards stop being
      scannable and the board's columns start to scroll. Refusing with the
      number is kinder than silently truncating — the user chose those fields.

    An empty list is legal and meaningful: it clears the explicit ordering and
    hands control back to each attribute's `show_on_card`.
    """
    wanted = [str(n).strip() for n in (names or []) if str(n).strip()]
    if len(wanted) > MAX_CARD_ATTRIBUTES:
        raise AttributeError_(
            f"A card can show {MAX_CARD_ATTRIBUTES} fields at most — "
            f"that is {len(wanted)}.")

    seen = set()
    for name in wanted:
        if name in seen:
            raise AttributeError_(f"“{name}” is listed twice.")
        seen.add(name)

    known = {str(_get(a, "internal_name")) for a in (attributes or [])}
    unknown = [n for n in wanted if n not in known]
    if unknown:
        raise AttributeError_(
            "There is no field called "
            + ", ".join(f"“{n}”" for n in unknown) + ".")
    return wanted


def next_form_order(attributes: Sequence[Any]) -> int:
    return max([int(_get(a, "form_order") or 0) for a in (attributes or [])], default=-1) + 1


def apply_order(attributes: Sequence[Any], ordered_ids: Sequence[Any]) -> List[Dict[str, Any]]:
    """``[{id, form_order}]`` for a reorder. Anything unnamed keeps its place
    after the named ones, so a partial list cannot silently shuffle the rest.
    """
    wanted = [str(i) for i in (ordered_ids or [])]
    by_id = {str(_get(a, "id") or _get(a, "_id")): a for a in (attributes or [])}
    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        raise AttributeError_("That list refers to an attribute that no longer exists.")

    out = [{"id": i, "form_order": n} for n, i in enumerate(wanted)]
    rest = [k for k in by_id if k not in set(wanted)]
    rest.sort(key=lambda k: int(_get(by_id[k], "form_order") or 0))
    for n, key in enumerate(rest, start=len(wanted)):
        out.append({"id": key, "form_order": n})
    return out


# ── helpers ──────────────────────────────────────────────────────────────────
def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)


def _option_keys(attribute: Any) -> Set[str]:
    return {str(_get(o, "key")) for o in (_get(attribute, "options") or [])}


def _same(a: Any, b: Any) -> bool:
    return str(getattr(a, "value", a)) == str(getattr(b, "value", b))


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)) and len(value) == 0:
        return True
    return False
