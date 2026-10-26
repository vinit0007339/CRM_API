"""Pure rules for the CRM filter builder. E22.

WHAT THIS IS. The list screens ship a fixed set of query parameters — `q`,
`owner_id`, `industry`, `amount_min` and so on. That set cannot express "deals
over ₹5L closing this quarter that nobody owns", and it can never reach an
attribute the account defined itself. This module turns a filter GROUP the user
built in the UI into a Mongo fragment.

    {"match": "all",
     "conditions": [{"field": "amount", "operator": "gte", "value": 500000},
                    {"field": "region", "operator": "is", "value": "South"}]}

⚠️ THREE THINGS THIS MODULE EXISTS TO GUARANTEE.

1. **It cannot escape the account.** It returns a fragment to be merged UNDER a
   tenant-scoped filter and never emits `created_by`, `$where`, `$expr` or any
   operator that can name another field. `queries.assert_account_scoped` still
   runs on the merged result.

2. **An unknown field is refused, not ignored.** A filter that silently drops a
   condition shows a list that looks filtered and is not — the user reads a
   subset as if it were the whole set and acts on it. Every condition is
   resolved against a field catalogue or the whole request fails.

3. **Custom attributes and typed columns read the same.** `industry` is a typed
   column, `region` lives in `custom_fields`. The caller passes the account's
   attribute rows; this module decides where each field actually lives, exactly
   as `attribute_routing` does on the write path.

Everything here is a pure function over plain dicts — no database, no request —
so the rules can be tested directly, the way ``stage_rules`` and
``lost_reason_rules`` are.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_CONDITIONS = 20

# The column a field lives in when it is NOT a custom attribute. Kept in step
# with `attribute_routing.SYSTEM_FIELDS` plus the read-only columns a list can
# filter on but a form cannot write.
SYSTEM_FIELDS: Dict[str, Dict[str, str]] = {
    "company": {
        "name": "text", "domain": "text", "website": "url",
        "linkedin_url": "url", "industry": "text", "employee_count": "number",
        "annual_revenue": "number", "currency": "text", "phone": "phone",
        "description": "textarea", "owner_id": "user",
        "is_archived": "boolean", "created_at": "datetime",
        "updated_at": "datetime", "last_activity_at": "datetime",
        # Activity, derived — see DERIVED_FIELDS.
        "has_open_task": "boolean", "has_overdue_task": "boolean",
        "next_activity_at": "datetime",
    },
    "deal": {
        "name": "text", "amount": "number", "currency": "text",
        "expected_close_date": "date", "actual_close_date": "date",
        "description": "textarea", "owner_id": "user", "status": "text",
        "stage_id": "text", "pipeline_id": "text", "probability": "number",
        "source": "text", "created_at": "datetime", "updated_at": "datetime",
        "last_activity_at": "datetime", "stage_entered_at": "datetime",
        # Activity, derived — see DERIVED_FIELDS. `is_rotting` is the one that
        # needs context beyond the record: it reads the deal's STAGE threshold.
        "has_open_task": "boolean", "has_overdue_task": "boolean",
        "is_rotting": "boolean", "next_activity_at": "datetime",
    },
}

# CRM-411. Fields that are NOT a column of their own name.
#
# Each counter is already denormalised onto the record and reconciled by
# `scripts/reconcile_crm_task_counters.py`, so these are a rewrite rather than a
# join: "has an overdue task" is `overdue_task_count > 0`. They existed on every
# record and in no catalogue, which is why Brevo's `Activities > Has overdue
# task` had no equivalent here.
#
# ⚠️ A DERIVED FIELD IS STILL A REAL FIELD. It goes through the same catalogue,
# the same operator check and the same account scoping as a column — the only
# difference is which clause it compiles to.
COUNTER_FOR: Dict[str, str] = {
    "has_open_task": "open_task_count",
    "has_overdue_task": "overdue_task_count",
}

# `is_rotting` is compiled separately because it is the only field whose answer
# depends on something that is not on the record: the stage's `rotting_days`.
ROTTING_FIELD = "is_rotting"

DERIVED_FIELDS = frozenset(COUNTER_FOR) | {ROTTING_FIELD}

# ⚠️ A DERIVED BOOLEAN HAS NO "EMPTY" STATE, so the catalogue must not offer
# one. `OPERATORS["boolean"]` includes is_empty/is_not_empty because a STORED
# boolean column can genuinely be null — `is_archived` on a record written
# before the field existed. These are computed from a counter or a timestamp
# and are always exactly true or false.
#
# `test_e22_filters.test_every_catalogue_field_is_actually_filterable` caught
# this: the catalogue advertised an operator the compiler then refused, which
# is the one thing that invariant exists to prevent. Narrowing the catalogue is
# the fix, not loosening the compiler — the alternative is an operator in the
# dropdown that answers a question nobody asked.
DERIVED_BOOLEAN_OPERATORS: Tuple[str, ...] = ("is", "is_not")

# Labels the generic rule would mangle. `_system_label` turns
# "has_overdue_task" into "Has overdue task", which is fine, but "is_rotting"
# into "Is rotting" and "next_activity_at" into "Next activity at", which read
# like database columns rather than questions.
FIELD_LABELS: Dict[str, str] = {
    "has_open_task": "Has an open task",
    "has_overdue_task": "Has an overdue task",
    "is_rotting": "Is rotting",
    "next_activity_at": "Next activity",
    "last_activity_at": "Last activity",
}

# Which operators each type offers. The UI reads the same table, so a user is
# never shown an operator the server would then refuse.
OPERATORS: Dict[str, Tuple[str, ...]] = {
    "text": ("is", "is_not", "contains", "not_contains", "starts_with",
             "is_empty", "is_not_empty"),
    "textarea": ("contains", "not_contains", "is_empty", "is_not_empty"),
    "url": ("is", "contains", "is_empty", "is_not_empty"),
    "email": ("is", "contains", "is_empty", "is_not_empty"),
    "phone": ("is", "contains", "is_empty", "is_not_empty"),
    "number": ("eq", "neq", "gt", "gte", "lt", "lte", "between",
               "is_empty", "is_not_empty"),
    "currency": ("eq", "neq", "gt", "gte", "lt", "lte", "between",
                 "is_empty", "is_not_empty"),
    "date": ("on", "before", "after", "between", "is_empty", "is_not_empty"),
    "datetime": ("on", "before", "after", "between", "is_empty", "is_not_empty"),
    "boolean": ("is", "is_empty", "is_not_empty"),
    "single_select": ("is", "is_not", "in", "is_empty", "is_not_empty"),
    "multi_select": ("has_any", "has_all", "is_empty", "is_not_empty"),
    "user": ("is", "is_not", "in", "is_empty", "is_not_empty"),
}

# Operators that take no value at all. Sending one with a value is not an
# error; the value is simply unused.
NO_VALUE = frozenset({"is_empty", "is_not_empty"})
# Operators whose value is a two-item [low, high].
RANGE_OPS = frozenset({"between"})
# Operators whose value is a list.
LIST_OPS = frozenset({"in", "has_any", "has_all"})


def escape(value: Any) -> str:
    """A literal for a `$regex`, with every metacharacter neutralised.

    ⚠️ WITHOUT THIS A SEARCH BOX IS A QUERY LANGUAGE. A user typing `.*` into
    "contains" would match every row, and a pathological pattern turns a list
    request into a CPU burner on the database.
    """
    return re.escape(str(value))


def field_catalogue(object_type: str,
                    attributes: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    """Every field this object can be filtered on, with its type and operators.

    The UI renders from exactly this, so what a user can build is what the
    server can answer. System columns come first in a stable order; the
    account's own attributes follow in their form order.
    """
    out: List[Dict[str, Any]] = []
    for name, kind in SYSTEM_FIELDS.get(object_type, {}).items():
        operators = (DERIVED_BOOLEAN_OPERATORS
                     if name in DERIVED_FIELDS and kind == "boolean"
                     else OPERATORS.get(kind, ()))
        out.append({
            "field": name, "label": _system_label(name), "type": kind,
            "source": "column", "operators": list(operators),
            "options": [],
        })

    seen = {row["field"] for row in out}
    rows = sorted(
        [a for a in (attributes or []) if _attr(a, "internal_name")],
        key=lambda a: (_attr(a, "form_order") or 0, str(_attr(a, "label") or "")),
    )
    for attribute in rows:
        name = str(_attr(attribute, "internal_name"))
        if name in seen:
            continue
        seen.add(name)
        kind = _type_of(attribute)
        out.append({
            "field": name,
            "label": str(_attr(attribute, "label") or name),
            "type": kind,
            "source": "custom",
            "operators": list(OPERATORS.get(kind, ())),
            "options": [
                {"key": _attr(o, "key"), "label": _attr(o, "label")}
                for o in (_attr(attribute, "options") or [])
            ],
        })
    return out


def references(raw: Any, field: str) -> bool:
    """Does this filter group mention ``field``? CRM-411.

    Lets a route decide whether to fetch the extra data a derived field needs
    instead of fetching it on every request just in case. A malformed group
    answers False — `build` will refuse it with a proper message a moment
    later, and this is not the place to report that.
    """
    group, error = parse(raw)
    if error or not group:
        return False
    for condition in (_attr(group, "conditions") or []):
        if str(_attr(condition, "field") or "").strip() == field:
            return True
    return False


def _system_label(name: str) -> str:
    if name in FIELD_LABELS:
        return FIELD_LABELS[name]
    return name.replace("_id", "").replace("_", " ").strip().capitalize()


def _attr(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _type_of(attribute: Any) -> str:
    raw = _attr(attribute, "attribute_type", "text")
    # ⚠️ `str()` ON A str-ENUM RETURNS "AttributeType.TEXT". This codebase has
    # been bitten by that four times; `.value` is the only safe read.
    return str(getattr(raw, "value", raw) or "text")


def parse(raw: Any) -> Tuple[Any, Optional[str]]:
    """A filter group out of the `filters` query parameter.

    The group is a nested structure, so it travels as JSON in one query
    parameter rather than as a flat set of `field_0`, `op_0`, `value_0` keys
    that the server would have to re-pair and that cannot express `match`.

    ⚠️ A MALFORMED PARAMETER MUST NOT RETURN AN UNFILTERED LIST. That is the
    same lie as a dropped condition, from a different direction — so a broken
    group is an error, while an ABSENT one is honestly "no filter".
    """
    if raw in (None, "", b""):
        return None, None
    if isinstance(raw, dict):
        return raw, None
    try:
        group = json.loads(raw)
    except (TypeError, ValueError):
        return None, "That filter could not be read. Please rebuild it."
    if group in (None, {}, []):
        return None, None
    if not isinstance(group, dict):
        return None, "A filter is a group of conditions."
    conditions = group.get("conditions")
    if conditions is not None and not isinstance(conditions, list):
        return None, "A filter is a group of conditions."
    return group, None


def build(group: Any, object_type: str,
          attributes: Optional[Sequence[Any]] = None,
          context: Optional[Dict[str, Any]] = None
          ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fold a filter group into a Mongo fragment.

    Returns ``(fragment, None)`` or ``(None, message)``. The message is
    user-facing: the route answers a business failure with HTTP 200 and
    ``status: "failure"``.

    An empty or absent group is not an error — it is "no filter", and returns
    ``({}, None)`` so the caller can merge unconditionally.
    """
    if not group:
        return {}, None

    conditions = _attr(group, "conditions") or []
    if not conditions:
        return {}, None
    if len(conditions) > MAX_CONDITIONS:
        return None, (f"That is {len(conditions)} conditions — "
                      f"a filter stops being readable past {MAX_CONDITIONS}.")

    match = str(_attr(group, "match", "all") or "all").lower()
    if match not in ("all", "any"):
        return None, "A filter matches either all or any of its conditions."

    catalogue = {row["field"]: row for row in field_catalogue(object_type, attributes)}

    clauses: List[Dict[str, Any]] = []
    for condition in conditions:
        clause, error = _one(condition, catalogue, context)
        if error:
            return None, error
        if clause:
            clauses.append(clause)

    if not clauses:
        return {}, None
    if len(clauses) == 1 and match == "all":
        return clauses[0], None
    return {"$and" if match == "all" else "$or": clauses}, None


def _one(condition: Any, catalogue: Dict[str, Dict[str, Any]],
         context: Optional[Dict[str, Any]] = None
         ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    name = str(_attr(condition, "field") or "").strip()
    if not name:
        return None, "A condition needs a field."

    spec = catalogue.get(name)
    if not spec:
        # ⚠️ REFUSED, NOT SKIPPED. A dropped condition shows a list that looks
        # filtered and is not, which is worse than an error nobody can miss.
        return None, f"There is no field called “{name}” to filter on."

    operator = str(_attr(condition, "operator") or "").strip().lower()
    if operator not in spec["operators"]:
        return None, (f"“{spec['label']}” cannot be filtered with "
                      f"“{operator or 'that'}”.")

    # CRM-411. Derived fields are checked AFTER the operator, so an impossible
    # operator is still reported as an operator problem rather than as a
    # missing field.
    #
    # No `and spec["source"] == "column"` guard: `field_catalogue` adds system
    # fields first and skips any custom attribute whose name it has already
    # seen, so a derived name can never arrive here as a custom one. The guard
    # was there and mutation testing showed it could not fail — an untestable
    # condition is a claim nobody can check. The shadowing invariant has its
    # own test in both filter suites instead.
    if name in DERIVED_FIELDS:
        return _derived(name, operator, _attr(condition, "value"), spec, context)

    path = name if spec["source"] == "column" else f"custom_fields.{name}"
    return _clause(path, operator, _attr(condition, "value"), spec)


def _wanted(operator: str, value: Any) -> bool:
    """What a boolean condition is actually asking for.

    ``is_not true`` and ``is false`` are the same question, and a user builds
    both. Collapsing them here means the two cannot compile differently.
    """
    truthy = value if isinstance(value, bool) else str(value).strip().lower() in (
        "true", "1", "yes")
    return truthy if operator in ("is", "eq", "==") else not truthy


def _derived(name: str, operator: str, value: Any, spec: Dict[str, Any],
             context: Optional[Dict[str, Any]]
             ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Compile a field that is not a column of its own name. CRM-411."""
    if operator in NO_VALUE:
        # ⚠️ NOT REACHABLE THROUGH `build` — `_one` checks the operator against
        # the catalogue first, and DERIVED_BOOLEAN_OPERATORS no longer lists
        # these. Kept as a belt for a direct caller, and so that widening the
        # catalogue later cannot silently start answering "is empty" on
        # something computed, which has no answer. Covered by a test that
        # asserts the EARLIER refusal, so if this ever becomes reachable the
        # test is what tells you.
        return None, f"“{spec['label']}” is a yes/no field — use is or is not."

    wanted = _wanted(operator, value)

    counter = COUNTER_FOR.get(name)
    if counter:
        if wanted:
            return {counter: {"$gt": 0}}, None
        # ⚠️ NOT `{counter: 0}`. The counters were added after the collections
        # existed, so a record written before them has NO KEY — and `{x: 0}`
        # does not match a missing field. Without the `$exists` arm, "has no
        # overdue task" would silently hide every older record.
        return {"$or": [{counter: 0}, {counter: {"$exists": False}}]}, None

    # `is_rotting` — the only field whose answer is not on the record.
    stages = (context or {}).get("stages")
    now = (context or {}).get("now") or datetime.now(timezone.utc)
    if not stages:
        # Refused, never silently dropped. Answering "no deals are rotting"
        # because the caller forgot to pass stages is a lie the user cannot see.
        return None, ("Rotting can only be filtered within a pipeline — "
                      "choose one and try again.")

    from app.services import stage_rules

    arms = []
    for stage in stages:
        row = stage if isinstance(stage, dict) else {
            "id": getattr(stage, "id", None),
            "type": getattr(stage, "type", None),
            "rotting_days": getattr(stage, "rotting_days", None),
        }
        cutoff = stage_rules.rotting_cutoff(row, now)
        if cutoff is None:
            continue
        arms.append({"stage_id": row.get("id"),
                     "stage_entered_at": {"$lte": cutoff}})

    if wanted:
        if not arms:
            # No stage can rot, so nothing is rotting. A clause that cannot
            # match is the honest answer — and `$or: []` is a Mongo error.
            return {"_id": {"$in": []}}, None
        return ({"$or": arms} if len(arms) > 1 else arms[0]), None

    # "is not rotting" is the complement, including every stage with no
    # threshold and every closed deal.
    if not arms:
        return {}, None
    return {"$nor": arms}, None


def _clause(path: str, operator: str, value: Any, spec: Dict[str, Any]
            ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    kind = spec["type"]

    if operator == "is_empty":
        # A typed column that was never set is null; a custom key is absent.
        # Both have to count as empty or the filter lies about half the rows.
        return {"$or": [{path: None}, {path: ""}, {path: {"$exists": False}}]}, None
    if operator == "is_not_empty":
        return {path: {"$nin": [None, ""], "$exists": True}}, None

    if operator in RANGE_OPS:
        pair = list(value or [])
        if len(pair) != 2:
            return None, f"“{spec['label']}” between needs a low and a high value."
        lo, error = _coerce(pair[0], kind, spec)
        if error:
            return None, error
        hi, error = _coerce(pair[1], kind, spec)
        if error:
            return None, error
        if lo is None or hi is None:
            return None, f"“{spec['label']}” between needs both ends."
        return {path: {"$gte": lo, "$lte": hi}}, None

    if operator in LIST_OPS:
        raw = value if isinstance(value, (list, tuple)) else [value]
        items = []
        for item in raw:
            coerced, error = _coerce(item, kind, spec)
            if error:
                return None, error
            if coerced is not None:
                items.append(coerced)
        if not items:
            return None, f"Choose at least one value for “{spec['label']}”."
        if operator == "has_all":
            return {path: {"$all": items}}, None
        return {path: {"$in": items}}, None

    coerced, error = _coerce(value, kind, spec)
    if error:
        return None, error
    if coerced is None:
        return None, f"“{spec['label']}” needs a value."

    if operator in ("is", "eq", "on"):
        if kind in ("date", "datetime") and operator == "on":
            # "on" is a DAY, not an instant. Comparing a stored timestamp to
            # midnight matches nothing, so this is a half-open day window.
            return {path: {"$gte": coerced, "$lt": _end_of_day(coerced)}}, None
        return {path: coerced}, None
    if operator in ("is_not", "neq"):
        return {path: {"$ne": coerced}}, None
    if operator in ("gt", "after"):
        return {path: {"$gt": coerced}}, None
    if operator == "gte":
        return {path: {"$gte": coerced}}, None
    if operator in ("lt", "before"):
        return {path: {"$lt": coerced}}, None
    if operator == "lte":
        return {path: {"$lte": coerced}}, None
    if operator == "contains":
        return {path: {"$regex": escape(coerced), "$options": "i"}}, None
    if operator == "not_contains":
        return {path: {"$not": re.compile(escape(coerced), re.I)}}, None
    if operator == "starts_with":
        return {path: {"$regex": f"^{escape(coerced)}", "$options": "i"}}, None

    return None, f"“{operator}” is not an operator this filter understands."


def _end_of_day(value: Any) -> Any:
    if isinstance(value, datetime):
        start = value.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


def _coerce(value: Any, kind: str, spec: Dict[str, Any]
            ) -> Tuple[Any, Optional[str]]:
    """A filter value in the type the stored column actually holds.

    A number typed into a text box arrives as "500000". Compared against an
    `Optional[float]` column as a string, Mongo answers nothing at all and the
    user sees an empty list with no error — the worst possible failure for a
    filter.
    """
    if value is None or value == "":
        return None, None

    if kind in ("number", "currency"):
        try:
            text = str(value).replace(",", "").strip()
            number = float(text)
        except (TypeError, ValueError):
            return None, f"“{spec['label']}” takes a number."
        return (int(number) if number.is_integer() else number), None

    if kind == "boolean":
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in ("true", "yes", "1"):
            return True, None
        if text in ("false", "no", "0"):
            return False, None
        return None, f"“{spec['label']}” takes yes or no."

    if kind in ("date", "datetime"):
        if isinstance(value, datetime):
            return _aware(value), None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            return _aware(datetime.fromisoformat(text)), None
        except ValueError:
            return None, f"“{spec['label']}” takes a date."

    return str(value), None


def _aware(value: datetime) -> datetime:
    """Naive input is read as UTC — the only timezone stored values carry."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
