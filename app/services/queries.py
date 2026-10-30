"""Mongo filter, sort and aggregation builders for the CRM module. CRM-019.

Pure functions: they assemble plain dicts and take ids that the caller has
already normalised. No Beanie, no Motor, no pydantic — so the query shapes can
be unit-tested, which matters more here than anywhere else in the module:

* **NFR5** says every CRM query must filter on ``created_by`` as the leading
  index key. That is a claim you can only really make if it is tested, and
  ``assert_account_scoped`` below plus the test-suite check make it true by
  construction rather than by review.
* ``list()`` and ``count()`` must apply *identical* filters. Elsewhere in this
  codebase they are hand-copied into both methods, which is exactly how a list
  showing 20 rows above a "25 results" total happens. Both call the same
  builder here.

Search values are ``re.escape``d. (Note: the existing ``ListCrud.list`` passes
the raw ``q`` straight into ``$regex``, so a user searching for ``a.b`` gets
pattern semantics. Worth a follow-up ticket; the CRM module does not repeat it.)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

ASCENDING = 1
DESCENDING = -1

# Fields the client may sort by, per object. An unknown sort_by falls back to
# the default rather than being passed through to Mongo.
COMPANY_SORT_FIELDS = frozenset({
    "name", "domain", "industry", "employee_count", "annual_revenue",
    "contact_count", "deal_count", "open_deal_value", "won_deal_value",
    "created_at", "updated_at", "last_activity_at",
})
DEAL_SORT_FIELDS = frozenset({
    "name", "amount", "expected_close_date", "actual_close_date", "status",
    "position", "created_at", "updated_at", "last_activity_at",
    "stage_entered_at",
})


def contains(value: str) -> Dict[str, str]:
    """Case-insensitive 'contains', with the needle escaped."""
    return {"$regex": re.escape(str(value).strip()), "$options": "i"}


def _or_contains(fields: Sequence[str], value: str) -> Dict[str, Any]:
    return {"$or": [{f: contains(value)} for f in fields]}


def _range(lo: Any = None, hi: Any = None) -> Optional[Dict[str, Any]]:
    out: Dict[str, Any] = {}
    if lo is not None:
        out["$gte"] = lo
    if hi is not None:
        out["$lte"] = hi
    return out or None


def _base(account_id: Any) -> Dict[str, Any]:
    """Every CRM filter starts here. Never call a builder without an account."""
    if account_id is None:
        raise ValueError("account_id is required — CRM queries are always tenant-scoped")
    return {"created_by": account_id}


def assert_account_scoped(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Guard: ``created_by`` present and first. Cheap enough to keep in prod."""
    if "created_by" not in filters:
        raise ValueError("CRM query is missing its created_by tenant filter")
    if filters["created_by"] is None:
        raise ValueError("CRM query has a null created_by — that matches the wrong documents")
    if next(iter(filters)) != "created_by":
        raise ValueError("created_by must be the leading key so the index is used")
    return filters


def merge_fragment(filters: Dict[str, Any],
                   fragment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """AND a filter-builder fragment (E22) onto a tenant-scoped filter.

    ⚠️ NOT ``dict.update``. The ``q`` arm already occupies ``$or``, and a
    group with ``match: "any"`` produces another one — ``update`` would
    silently throw the search away and WIDEN the result set, which is the one
    way a filter can lie without anybody noticing. On any shared key both
    sides move into ``$and``, where both must hold.

    ``created_by`` is re-seated as the leading key afterwards, so NFR5 still
    holds and ``assert_account_scoped`` still passes. A fragment that names
    ``created_by`` is refused outright: that is the only way out of the
    account, and ``filter_rules`` never emits it.
    """
    if not fragment:
        return assert_account_scoped(filters)
    if "created_by" in fragment:
        raise ValueError("a filter fragment may not name created_by")
    if set(fragment) & set(filters):
        account = filters["created_by"]
        rest = {k: v for k, v in filters.items() if k != "created_by"}
        arms = [arm for arm in (rest, fragment) if arm]
        return assert_account_scoped({"created_by": account, "$and": arms})
    filters.update(fragment)
    return assert_account_scoped(filters)


# ── companies ────────────────────────────────────────────────────────────────
def company_filter(
    *,
    account_id: Any,
    q: Optional[str] = None,
    owner_id: Any = None,
    industry: Optional[str] = None,
    tag_ids: Optional[Sequence[Any]] = None,
    domain: Optional[str] = None,
    is_archived: Optional[bool] = False,
    created_from: Any = None,
    created_to: Any = None,
    ids: Optional[Sequence[Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Filter for the companies list. ``is_archived=None`` includes archived.

    ``extra`` is a fragment from ``filter_rules.build`` — the advanced filter
    builder. It is merged, not updated in: see ``merge_fragment``.
    """
    f = _base(account_id)
    if is_archived is not None:
        f["is_archived"] = bool(is_archived)
    if owner_id is not None:
        f["owner_id"] = owner_id
    if industry:
        f["industry"] = industry
    if tag_ids:
        f["tag_ids"] = {"$in": list(tag_ids)}
    if domain:
        f["domain"] = domain
    created = _range(created_from, created_to)
    if created:
        f["created_at"] = created
    if ids is not None:
        f["_id"] = {"$in": list(ids)}
    if q:
        f.update(_or_contains(["name", "domain"], q))
    return merge_fragment(f, extra)


# ── contacts (E25) ─────────────────────────────────────────────
def contact_filter(
    *,
    account_id: Any,
    q: Optional[str] = None,
    owner_id: Any = None,
    company_id: Any = None,
    lifecycle_stage: Optional[str] = None,
    tag_ids: Optional[Sequence[Any]] = None,
    email: Optional[str] = None,
    linked: Optional[bool] = None,
    is_archived: Optional[bool] = False,
    created_from: Any = None,
    created_to: Any = None,
    ids: Optional[Sequence[Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Filter for the contacts list.

    ``email`` is matched EXACTLY and is expected ALREADY NORMALISED — it is
    the per-account dedupe key, served by ``uniq_crm_contact_email``. A
    substring search on an address belongs in ``q``.

    ``linked`` is the platform link: True = marketable (has a ``contact_id``),
    False = CRM-only. Note the ``$ne: None`` arm rather than ``$exists``: a
    contact written before the field existed has no key at all, and
    ``$exists: True`` would call it linked.
    """
    f = _base(account_id)
    if is_archived is not None:
        f["is_archived"] = bool(is_archived)
    if owner_id is not None:
        f["owner_id"] = owner_id
    if company_id is not None:
        f["primary_company_id"] = company_id
    if lifecycle_stage:
        f["lifecycle_stage"] = lifecycle_stage
    if tag_ids:
        f["tag_ids"] = {"$in": list(tag_ids)}
    if email:
        f["email"] = email
    if linked is not None:
        f["contact_id"] = {"$ne": None} if linked else None
    created = _range(created_from, created_to)
    if created:
        f["created_at"] = created
    if ids is not None:
        f["_id"] = {"$in": list(ids)}
    if q:
        f.update(_or_contains(["full_name", "email", "primary_company_name"], q))
    return merge_fragment(f, extra)


CONTACT_SORT_FIELDS = frozenset({
    "full_name", "email", "job_title", "lifecycle_stage", "deal_count",
    "open_deal_value", "created_at", "updated_at", "last_activity_at",
})


def contact_sort(sort_by: Optional[str], sort_dir: str = "desc"):
    """Sort for the contacts list. An unknown field falls back to the default.

    Name ASCENDING by default, unlike companies and deals: a contact list is
    read as a directory, and newest-first makes looking somebody up a scroll.
    """
    direction = ASCENDING if str(sort_dir).lower() == "asc" else DESCENDING
    if sort_by in CONTACT_SORT_FIELDS:
        return [(sort_by, direction)]
    return [("full_name", ASCENDING)]


# ── unified contact profiles (contact unification, phase 4) ──────────────────

def profile_filter(
    *,
    account_id: Any,
    q: Optional[str] = None,
    owner_id: Any = None,
    company_id: Any = None,
    lifecycle_stage: Optional[str] = None,
    tag_ids: Optional[Sequence[Any]] = None,
    contact_ids: Optional[Sequence[Any]] = None,
    is_archived: Optional[bool] = False,
    created_from: Any = None,
    created_to: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Filter for the unified contacts list, over ``crm_contact_profiles``.

    ⚠️ **DELIBERATELY THE SAME SHAPE AS ``contact_filter``.** The two serve the
    same screen on either side of the cutover flag, so a filter that narrows
    one must narrow the other identically — otherwise flipping an account's
    flag silently changes which contacts it can find, and the bug report is
    "search is broken since Tuesday" with no code change to point at. Keep the
    parameter lists in step.

    ⚠️ **``linked`` IS GONE, AND ITS ABSENCE IS THE POINT.** On ``crm_contacts``
    it asked whether a CRM person had been joined to the marketing audience.
    Here every row IS a marketing contact, so the question has no meaning —
    accepting it and ignoring it would be worse than refusing it. The route
    drops the parameter when unified rather than passing it through.

    ``q`` searches the identity cache plus the denormalised company name,
    matching ``contact_filter`` field for field.
    """
    f = _base(account_id)
    if is_archived is not None:
        f["is_archived"] = bool(is_archived)
    if owner_id is not None:
        f["owner_id"] = owner_id
    if company_id is not None:
        f["primary_company_id"] = company_id
    if lifecycle_stage:
        f["lifecycle_stage"] = lifecycle_stage
    if tag_ids:
        f["tag_ids"] = {"$in": list(tag_ids)}
    if contact_ids is not None:
        f["contact_id"] = {"$in": list(contact_ids)}
    created = _range(created_from, created_to)
    if created:
        f["created_at"] = created
    if q:
        f.update(_or_contains(["full_name", "email", "primary_company_name"], q))
    return merge_fragment(f, extra)


#: ⚠️ A SUBSET OF ``CONTACT_SORT_FIELDS``, and every member must be a field
#: this collection actually holds. `full_name`, `email` and `job_title` are
#: here because the identity cache carries them; anything else a caller asks
#: for falls back to the default rather than sorting on a key that is absent
#: from every document — which Mongo answers by returning the collection in
#: natural order, i.e. a list that looks shuffled rather than wrong.
PROFILE_SORT_FIELDS = frozenset({
    "full_name", "email", "job_title", "lifecycle_stage", "deal_count",
    "open_deal_value", "created_at", "updated_at", "last_activity_at",
})


def profile_sort(sort_by: Optional[str], sort_dir: str = "desc"):
    """Sort for the unified contacts list. Name ASCENDING by default.

    Same default and same fallback as ``contact_sort``: the list is read as a
    directory, so newest-first makes looking somebody up a scroll.
    """
    direction = ASCENDING if str(sort_dir).lower() == "asc" else DESCENDING
    if sort_by in PROFILE_SORT_FIELDS:
        return [(sort_by, direction)]
    return [("full_name", ASCENDING)]


# ── saved views (E23) ─────────────────────────────────────────────
def saved_view_filter(*, account_id: Any, object_type: Optional[str] = None,
                      user_id: Any = None) -> Dict[str, Any]:
    """Filter for the saved-view list.

    ``user_id`` narrows to ONE PERSON'S OWN views and is not how the toolbar
    reads — visibility is "mine plus shared", which
    ``saved_view_rules.visible_to`` decides over the fetched rows. This
    parameter exists for the ownership-scoped reads (clearing somebody's other
    defaults), where the account-wide read would be wrong.
    """
    f = _base(account_id)
    if object_type:
        f["object_type"] = object_type
    if user_id is not None:
        f["created_by_user_id"] = user_id
    return assert_account_scoped(f)


def company_dedupe_filter(
    *, account_id: Any, domain: Optional[str] = None, name: Optional[str] = None
) -> Dict[str, Any]:
    """Candidate lookup for duplicate detection (CRM-021).

    Domain match is index-served on ``(created_by, domain)``. The name arm is a
    prefix-free contains — deliberately loose, because ``dedupe.py`` does the
    real scoring and this only has to be a superset.
    """
    f = _base(account_id)
    f["is_archived"] = False
    arms: List[Dict[str, Any]] = []
    if domain:
        arms.append({"domain": domain})
    if name:
        # EQUALITY on the stored normalised name, served by
        # (created_by, name_normalised). This replaced a substring search for
        # the normalised needle inside the raw name, which could not match
        # "A.C.M.E. Ltd" against "Acme Limited" — both normalise to "acme", but
        # neither raw name contains the other — and scanned the collection in
        # order to fail.
        #
        # The `name` arm is kept as a fallback for rows written before the
        # field existed. `scripts/backfill_company_name_normalised.py` fills
        # them in; until it has run for an account, detection still works.
        arms.append({"name_normalised": name})
        arms.append({"name": contains(name), "name_normalised": None})
    if not arms:
        raise ValueError("dedupe lookup needs a domain or a name")
    if len(arms) == 1:
        f.update(arms[0])
    else:
        f["$or"] = arms
    return assert_account_scoped(f)


# ── deals ────────────────────────────────────────────────────────────────────
def deal_filter(
    *,
    account_id: Any,
    pipeline_id: Any = None,
    stage_id: Any = None,
    status: Optional[str] = None,
    owner_id: Any = None,
    company_id: Any = None,
    q: Optional[str] = None,
    tag_ids: Optional[Sequence[Any]] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    close_date_from: Any = None,
    close_date_to: Any = None,
    has_overdue_task: Optional[bool] = None,
    ids: Optional[Sequence[Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Filter for the deals list and for the board.

    ``pipeline_id=None`` means "all pipelines" — the List view's default and
    the one thing the board never does.
    """
    f = _base(account_id)
    if pipeline_id is not None:
        f["pipeline_id"] = pipeline_id
    if stage_id is not None:
        f["stage_id"] = stage_id
    if status:
        f["status"] = status
    if owner_id is not None:
        f["owner_id"] = owner_id
    if company_id is not None:
        f["primary_company_id"] = company_id
    if tag_ids:
        f["tag_ids"] = {"$in": list(tag_ids)}
    amount = _range(amount_min, amount_max)
    if amount:
        f["amount"] = amount
    close = _range(close_date_from, close_date_to)
    if close:
        f["expected_close_date"] = close
    if has_overdue_task:
        f["overdue_task_count"] = {"$gt": 0}
    if ids is not None:
        f["_id"] = {"$in": list(ids)}
    if q:
        f.update(_or_contains(["name", "primary_company_name"], q))
    return merge_fragment(f, extra)


def board_filter(*, account_id: Any, pipeline_id: Any, **kw) -> Dict[str, Any]:
    """The board is always one pipeline. Everything else is shared with the list."""
    if pipeline_id is None:
        raise ValueError("the board requires a pipeline_id — use the list view for all pipelines")
    return deal_filter(account_id=account_id, pipeline_id=pipeline_id, **kw)


def _stage_group_stage() -> Dict[str, Any]:
    """The ``$group`` body shared by the board totals and the list totals.

    ⚠️ ONE DEFINITION, DELIBERATELY. The board's column headers and the
    list's footer are the same arithmetic over a different ``$match``. Written
    twice, the two drift the first time either is corrected — and the symptom
    is a user reading two different pipeline values for the same deals on two
    screens, with nothing to say which is right.
    """
    return {
        "_id": "$stage_id",
        "deal_count": {"$sum": 1},
        "total_value": {"$sum": {"$ifNull": ["$amount", 0]}},
        # Weighted value has to honour a deal's OWN probability override where
        # it has one. Splitting the sum here is what makes that possible
        # without reading every card: deals WITH an override are weighted in
        # the database, deals without contribute their raw amount and are
        # weighted by the stage in forecast.py.
        "weighted_overridden": {"$sum": {
            "$cond": [
                {"$ne": [{"$ifNull": ["$probability", None]}, None]},
                {"$divide": [
                    {"$multiply": [{"$ifNull": ["$amount", 0]}, "$probability"]},
                    100,
                ]},
                0,
            ],
        }},
        "amount_without_override": {"$sum": {
            "$cond": [
                {"$ne": [{"$ifNull": ["$probability", None]}, None]},
                0,
                {"$ifNull": ["$amount", 0]},
            ],
        }},
    }


def _stage_currency_group_stage() -> Dict[str, Any]:
    """``_stage_group_stage`` keyed by stage AND currency. CRM-440 phase 3.

    ⚠️ THE LIST SPANS PIPELINES, AND PIPELINES HAVE DIFFERENT CURRENCIES. The
    board is safe to sum on its own — one pipeline, one currency (see
    `docs/currency.md`) — but the deals LIST has no pipeline filter by default,
    so `$sum: "$amount"` there adds INR to USD as bare numbers and the footer
    states the result as one figure. Adding `currency` to the key is what lets
    the caller report "₹4.2L · $5,000" instead of a number that is not true in
    any currency.

    ⚠️ A NULL CURRENCY IS ITS OWN GROUP, not a default. A lead whose pipeline
    never had one makes no claim; folding it into INR would be inventing the
    claim. It comes back under `null` and the caller decides how to show it.
    """
    group = dict(_stage_group_stage())
    group["_id"] = {"stage_id": "$stage_id",
                    "currency": {"$ifNull": ["$currency", None]}}
    return group


def list_totals_pipeline(*, account_id: Any, **kw) -> List[Dict[str, Any]]:
    """Per-stage sums over the DEALS LIST's filter — every pipeline, E24.

    ⚠️ THIS EXISTS SO THE FOOTER IS NOT A PAGE SUM. Adding up the 25 rows on
    screen and calling it the pipeline value is wrong by a factor of however
    many pages there are, and it is wrong silently — the number looks like a
    number. Grouped by stage because weighting needs the stage, and returned
    per stage so `forecast.list_summary` can weight each group with the
    pipeline definitions the route already has.
    """
    return [
        {"$match": deal_filter(account_id=account_id, **kw)},
        {"$group": _stage_currency_group_stage()},
    ]


def board_totals_pipeline(*, account_id: Any, pipeline_id: Any, **kw) -> List[Dict[str, Any]]:
    """Aggregation for per-stage counts and sums. One round trip for the whole board.

    Served by the ``(created_by, pipeline_id, stage_id, position)`` index —
    ``$match`` on the first two keys, ``$group`` on the third.
    """
    return [
        {"$match": board_filter(account_id=account_id, pipeline_id=pipeline_id, **kw)},
        {"$group": _stage_group_stage()},
    ]


def stage_cards_filter(*, account_id: Any, pipeline_id: Any, stage_id: Any, **kw) -> Dict[str, Any]:
    """One column's cards. Sort by ``position`` ascending — see board_sort()."""
    return deal_filter(
        account_id=account_id, pipeline_id=pipeline_id, stage_id=stage_id, **kw
    )


def board_sort() -> List[tuple]:
    """Card order within a column. Never changes — position IS the order."""
    return [("position", ASCENDING)]


def neighbours_filter(*, account_id: Any, pipeline_id: Any, stage_id: Any) -> Dict[str, Any]:
    """All cards in one column, for computing a drop position (CRM-037)."""
    f = _base(account_id)
    f["pipeline_id"] = pipeline_id
    f["stage_id"] = stage_id
    return assert_account_scoped(f)


# ── activities / associations / attributes ───────────────────────────────────
def activity_filter(
    *,
    account_id: Any,
    entity_type: Optional[str] = None,
    entity_id: Any = None,
    activity_type: Optional[str] = None,
    occurred_from: Any = None,
    occurred_to: Any = None,
) -> Dict[str, Any]:
    f = _base(account_id)
    if entity_type:
        f["entity_type"] = entity_type
    if entity_id is not None:
        f["entity_id"] = entity_id
    if activity_type:
        f["activity_type"] = activity_type
    occurred = _range(occurred_from, occurred_to)
    if occurred:
        f["occurred_at"] = occurred
    return assert_account_scoped(f)


def activity_sort() -> List[tuple]:
    return [("occurred_at", DESCENDING)]


def associations_of(*, account_id: Any, entity_type: str, entity_id: Any) -> Dict[str, Any]:
    """Every association touching one record, in either slot.

    Both arms are index-served: the forward one by the unique compound index,
    the reverse one by ``(created_by, to_type, to_id)``.
    """
    f = _base(account_id)
    f["$or"] = [
        {"from_type": entity_type, "from_id": entity_id},
        {"to_type": entity_type, "to_id": entity_id},
    ]
    return assert_account_scoped(f)


def attribute_filter(
    *, account_id: Any, object_type: Optional[str] = None, show_on_form: Optional[bool] = None
) -> Dict[str, Any]:
    f = _base(account_id)
    if object_type:
        f["object_type"] = object_type
    if show_on_form is not None:
        f["show_on_form"] = bool(show_on_form)
    return assert_account_scoped(f)


# ── sorting ──────────────────────────────────────────────────────────────────
def sort_expr(
    sort_by: Optional[str],
    sort_dir: Optional[str] = "desc",
    *,
    allowed: Sequence[str] = (),
    default_field: str = "created_at",
) -> List[tuple]:
    """``[(field, 1|-1)]``, falling back to the default for unknown fields.

    Never passes an unvetted client string through to Mongo.
    """
    field = sort_by if sort_by in set(allowed) else default_field
    direction = ASCENDING if str(sort_dir).lower() == "asc" else DESCENDING
    return [(field, direction)]


def company_sort(sort_by=None, sort_dir="desc") -> List[tuple]:
    return sort_expr(sort_by, sort_dir, allowed=COMPANY_SORT_FIELDS)


def deal_sort(sort_by=None, sort_dir="desc") -> List[tuple]:
    return sort_expr(sort_by, sort_dir, allowed=DEAL_SORT_FIELDS)


def paginate(skip: int = 0, limit: int = 50, *, max_limit: int = 200) -> tuple:
    """Clamp client-supplied paging. Mirrors the Query(...) bounds on the routes."""
    return max(0, int(skip or 0)), max(0, min(int(limit or 0), max_limit))
