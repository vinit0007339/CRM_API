"""Which columns a list view shows, and in what order. CRM-247 / CRM-248.

⚠️ **THE CHOICE SET IS ATTRIBUTES ∪ DERIVED COLUMNS, AND THAT UNION IS THE
WHOLE POINT OF THIS MODULE.** The attribute system describes *writable* fields
— the ones on the create form. But four of the eight columns the companies list
shows today (``contact_count``, ``deal_count``, ``open_deal_value``,
``last_activity_at``) are denormalised counters that no account can edit and no
attribute row describes. A picker built from ``GET /crm/attributes`` alone would
offer a choice set that silently cannot express the layout the user is already
looking at, and "customise your columns" would take four columns away.

**Unknown columns are refused on write and pruned on read.** The asymmetry is
deliberate:

* on write the client sent a name the account has no definition for, which is a
  bug worth surfacing rather than half-applying;
* on read the stored list is a snapshot and the world has moved — an admin
  deleted an attribute after somebody put it in their layout. Failing that read
  would leave the user with a list view that will not render until an
  administrator restores a field they meant to delete.

``name`` is locked. It is the cell that links to the record, so a layout without
it is a table with no way into any row.

Stdlib only. No DB, no pydantic.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

OBJECT_TYPES = ("company", "deal", "contact")

# Columns that cannot be removed: the one that opens the record.
#
# ⚠️ PER OBJECT TYPE, AND IT STAYS A MAPPING even though both entries are now
# `name`. It is keyed per type because a contact's locked column was
# `full_name`, not `name` — and collapsing this to one constant is how the
# next object type with a different identity column gets nothing locked and
# lets the picker remove the only cell that links into the row.
LOCKED_BY_OBJECT: Dict[str, Tuple[str, ...]] = {
    "company": ("name",),
    "deal": ("name",),
    # ⚠️ PER OBJECT TYPE, BECAUSE A CONTACT'S IS NOT `name`. A CRM contact has
    # `first_name` / `last_name` and a denormalised `full_name`; locking `name`
    # would lock a column that does not exist on it, so nothing would be locked
    # and the picker could remove the only cell that links into the row.
    "contact": ("full_name",),
}

# Back-compat: the original module-level constant, which was company/deal only.
# Call `locked_for(object_type)` in new code.
LOCKED_COLUMNS = LOCKED_BY_OBJECT["company"]


def locked_for(object_type: str = "company") -> Tuple[str, ...]:
    """The columns this object type cannot drop."""
    return LOCKED_BY_OBJECT.get(object_type, ())

# More than this and the table scrolls sideways past the point of being read.
# A cap the user is told about, never one applied silently — see `validate`.
MAX_COLUMNS = 15

# Read-only, derived or denormalised columns. NOT attributes: no account can
# edit them, `GET /crm/attributes` does not describe them, and without them the
# picker cannot express the layout the lists already ship with.
DERIVED_COLUMNS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "company": (
        ("contact_count", "Contacts"),
        ("deal_count", "Leads"),
        ("open_deal_value", "Open value"),
        ("last_activity_at", "Last activity"),
        ("created_at", "Created"),
    ),
    "deal": (
        # Hydrated from the pipeline at serialisation time — see
        # EXPORT_UNAVAILABLE below, which is where that costs something.
        ("stage_name", "Stage"),
        ("status", "Status"),
        ("primary_company_name", "Company"),
        ("last_activity_at", "Last activity"),
        ("created_at", "Created"),
    ),
    "contact": (
        # `full_name` is denormalised, not an attribute: it is derived from
        # first/last/email in `contact_rules.display_name` and no account can
        # edit it directly, but the list cannot render without it.
        ("full_name", "Name"),
        ("primary_company_name", "Company"),
        ("deal_count", "Leads"),
        ("open_deal_value", "Open value"),
        ("last_activity_at", "Last activity"),
        ("created_at", "Created"),
    ),
}

# Exactly what the two list views hard-coded before this epic, so a user who
# never opens the picker sees no change at all. `owner_id`, not `owner`: the
# canonical key is the attribute's internal_name — the same key the export
# picker and the import mapper use — and the renderer maps it to the `owner`
# ref the list payload carries.
DEFAULT_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "company": ("name", "industry", "employee_count", "owner_id",
                "contact_count", "deal_count", "open_deal_value",
                "last_activity_at"),
    "deal": ("name", "stage_name", "amount", "owner_id",
             "expected_close_date", "status", "last_activity_at"),
    "contact": ("full_name", "email", "job_title", "primary_company_name",
                "lifecycle_stage", "owner_id", "last_activity_at"),
}

# Columns a list can show but an export cannot produce. `stage_name` is
# resolved from the pipeline document when a deal is serialised; it is not
# stored on the deal, so the export writer — which reads model fields — has
# nothing to write. Named here so the export dialog can SAY a column was left
# out rather than quietly returning a narrower file than the screen implied.
EXPORT_UNAVAILABLE = frozenset({"stage_name"})

# Never a column: unbounded per record, or internal.
NEVER_COLUMNS = frozenset({
    "notes", "files", "activities", "emails", "custom_fields",
    "id", "_id", "created_by", "is_archived", "merged_into_id",
    "merge_snapshot", "name_normalised",
    # E25: the platform link id is internal plumbing — `is_linked` is the
    # column a user would actually want, and it is in DERIVED_COLUMNS.
    "contact_id", "full_name_normalised",
})


class ColumnError(Exception):
    """A layout that cannot be stored. The message is shown to the user."""


def available(attributes: Optional[Sequence[Any]] = None,
              object_type: str = "company") -> List[Dict[str, Any]]:
    """Everything the picker may offer, in the order it should be listed.

    Attributes first, in the account's own ``form_order`` and under the
    account's own labels — an account that renamed "Industry" to "Sector"
    should not be offered "Industry" here. Derived columns after, because they
    are the same for everybody.
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []

    for attribute in attributes or []:
        internal = _get(attribute, "internal_name")
        if not internal or internal in NEVER_COLUMNS or internal in seen:
            continue
        seen.add(internal)
        out.append({
            "value": internal,
            "label": _get(attribute, "label") or internal,
            "locked": internal in locked_for(object_type),
            "kind": "field",
        })

    # ⚠️ THE SYSTEM FIELDS ARE A FLOOR, NOT A CONSEQUENCE OF SEEDING.
    #
    # `name`, `industry`, `owner_id` and the rest are seeded as attribute rows
    # on first CRM use — but an account whose rows have not been seeded yet
    # still HAS those fields on its records. Without this, such an account got
    # a choice set of counters only, `resolve` filtered `name` out of the
    # defaults, and the Companies list rendered with no Name column at all.
    #
    # `export_rules.resolve_columns` has had the same fallback since E17, for
    # the same reason. The account's own rows above win on label and order;
    # this only fills what is missing.
    for internal, label in _system_fields(object_type):
        if internal in seen or internal in NEVER_COLUMNS:
            continue
        seen.add(internal)
        out.append({
            "value": internal,
            "label": label,
            "locked": internal in locked_for(object_type),
            "kind": "field",
        })

    for internal, label in DERIVED_COLUMNS.get(object_type, ()):
        if internal in seen:
            continue
        seen.add(internal)
        out.append({
            "value": internal,
            "label": label,
            "locked": internal in locked_for(object_type),
            "kind": "derived",
        })

    return out


def _system_fields(object_type: str):
    """``[(internal_name, label)]`` for the object type's built-in fields.

    Read from ``defaults`` rather than restated here: that module is already
    the single definition of what gets seeded, and a copy would drift the first
    time a field is added there.
    """
    from app.services.defaults import (
        SYSTEM_COMPANY_ATTRIBUTES, SYSTEM_DEAL_ATTRIBUTES,
    )

    source = (SYSTEM_DEAL_ATTRIBUTES if object_type == "deal"
              else SYSTEM_COMPANY_ATTRIBUTES)
    return [(row[0], row[1]) for row in source]


def known_columns(attributes: Optional[Sequence[Any]] = None,
                  object_type: str = "company") -> List[str]:
    """Just the names, for validation."""
    return [choice["value"] for choice in available(attributes, object_type)]


def validate(columns: Optional[Sequence[str]],
             attributes: Optional[Sequence[Any]] = None,
             object_type: str = "company") -> List[str]:
    """Check a layout on the way IN. Returns the cleaned list. Raises.

    Refuses rather than repairs: a client that sent a column this account has
    no definition for has a bug, and silently dropping it would store a layout
    the user did not choose and cannot see is different from what they asked
    for.

    The one thing it does repair is duplicates — a column listed twice is
    unambiguous about what was meant, and rendering it twice helps nobody.
    """
    if object_type not in OBJECT_TYPES:
        raise ColumnError(f'"{object_type}" isn\'t a CRM object type.')

    if not columns:
        raise ColumnError("Choose at least one column.")

    cleaned: List[str] = []
    for column in columns:
        name = str(column or "").strip()
        if not name:
            continue
        if name not in cleaned:
            cleaned.append(name)

    if not cleaned:
        raise ColumnError("Choose at least one column.")

    known = set(known_columns(attributes, object_type))
    unknown = [c for c in cleaned if c not in known]
    if unknown:
        raise ColumnError(
            f"{', '.join(unknown)} — no field by that name on this "
            f"{object_type}.")

    missing_locked = [c for c in locked_for(object_type) if c not in cleaned]
    if missing_locked:
        raise ColumnError(
            f"{', '.join(missing_locked)} can't be removed — it's the column "
            f"that opens the record.")

    if len(cleaned) > MAX_COLUMNS:
        # Said, not silently truncated: a layout that came back shorter than it
        # was sent, with no explanation, reads as data loss.
        raise ColumnError(
            f"That's {len(cleaned)} columns; {MAX_COLUMNS} is the most a list "
            f"can show. Remove {len(cleaned) - MAX_COLUMNS}.")

    return cleaned


def resolve(saved: Optional[Sequence[str]],
            attributes: Optional[Sequence[Any]] = None,
            object_type: str = "company") -> List[Dict[str, Any]]:
    """The layout to render, on the way OUT. Never raises.

    ``[{value, label, kind}]`` in the stored order.

    Prunes rather than refuses — see the module docstring. A stored column
    whose attribute has since been deleted disappears from the layout and the
    rest is kept: the user who chose seven columns and lost one should see six,
    not be reset to the defaults they had already rejected.

    Falls back to the defaults only when nothing but the locked columns
    survives, which is the case where "keep what's left" would hand back a
    one-column table.
    """
    choices = {c["value"]: c for c in available(attributes, object_type)}

    wanted = [c for c in (saved or []) if c in choices]
    # Dedupe defensively: this reads whatever is in the database, which is not
    # necessarily what `validate` last approved.
    ordered: List[str] = []
    for column in wanted:
        if column not in ordered:
            ordered.append(column)

    locked = locked_for(object_type)
    if not [c for c in ordered if c not in locked]:
        ordered = [c for c in DEFAULT_COLUMNS.get(object_type, ()) if c in choices]
        if not ordered:
            # An account whose attribute rows have not been seeded yet. The
            # locked column always resolves, so the table still renders.
            ordered = [c for c in locked if c in choices]

    # A layout that lost its link column — stored before it was locked, or
    # hand-edited — would render a table with no way into any row.
    for locked_column in reversed(locked):
        if locked_column not in ordered and locked_column in choices:
            ordered.insert(0, locked_column)

    return [choices[c] for c in ordered]


def is_default(columns: Optional[Sequence[str]],
               object_type: str = "company") -> bool:
    """Is this layout the shipped one? Drives whether "Reset" is offered."""
    return tuple(columns or ()) == DEFAULT_COLUMNS.get(object_type, ())


def exportable(columns: Optional[Sequence[str]]) -> Tuple[List[str], List[str]]:
    """``(exportable, left_out)`` for a layout handed to the export dialog.

    Returned as two lists rather than one filtered one so the dialog can name
    what it dropped. An export that comes back narrower than the screen, with
    no explanation, is indistinguishable from missing data.
    """
    keep = [c for c in (columns or []) if c not in EXPORT_UNAVAILABLE]
    dropped = [c for c in (columns or []) if c in EXPORT_UNAVAILABLE]
    return keep, dropped


def _get(obj: Any, field: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(field, default)
    return getattr(obj, field, default)
