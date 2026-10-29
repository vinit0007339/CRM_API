"""Pure rules for saved views. E23.

A saved view is a NAMED FILTER: the group the user built, plus the sort and the
columns that made it readable, under a name they can come back to. Brevo calls
these "segments" on contacts and saved filters on the deal list; the shape is
the same.

⚠️ FOUR THINGS THIS MODULE EXISTS TO DECIDE.

1. **Who may change a SHARED view.** A personal view belongs to one person. A
   shared one is on everybody's toolbar, so editing or deleting it changes what
   colleagues see — that is its creator's call, or an account owner's, and
   nobody else's. Without this rule "shared" quietly means "anyone can rewrite
   it", and a view people rely on changes under them.

2. **That a stored view is a SNAPSHOT, not a live reference.** It holds field
   NAMES, and an attribute can be deleted or renamed after it was saved. So the
   group is validated on WRITE, where refusing is useful, and only reported on
   READ — a view that has gone stale must still be listable and deletable, or
   the user is left with a broken button they cannot get rid of.

3. **One default per person per object.** Opening the deals list should land on
   the view they chose. Two defaults is not a tie to break at render time.

4. **A name is a name.** Trimmed, capped, and unique within the scope that will
   display it, because two views called "My pipeline" on one toolbar are
   indistinguishable.

No database, no request — the rules are unit-testable, same as
``filter_rules`` and ``lost_reason_rules``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services import filter_rules

OBJECT_TYPES = ("company", "deal", "contact")

NAME_MAX = 60
# Per account per object type. A toolbar stops being navigable long before
# this; the cap is here so one account cannot make the list endpoint slow for
# everybody by saving thousands.
MAX_VIEWS = 60

SORT_DIRS = ("asc", "desc")


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def clean_name(value: Any) -> Tuple[str, Optional[str]]:
    """``(name, error)``. Whitespace is not a name."""
    name = " ".join(str(value or "").split())
    if not name:
        return "", "Give this view a name."
    if len(name) > NAME_MAX:
        return "", f"A view name is at most {NAME_MAX} characters."
    return name, None


def clean_sort(sort_by: Any, sort_dir: Any) -> Tuple[Optional[str], str]:
    """A stored sort. An unknown direction falls back rather than failing —
    it is a display preference, not a filter, so it cannot show wrong rows."""
    field = str(sort_by).strip() if sort_by else None
    direction = str(sort_dir or "").strip().lower()
    return field or None, direction if direction in SORT_DIRS else "desc"


def is_shared(row: Any) -> bool:
    return bool(_get(row, "is_shared", False))


def owner_of(row: Any) -> str:
    return str(_get(row, "created_by_user_id") or "")


def visible_to(views: Sequence[Any], user_id: Any) -> List[Any]:
    """The views this person's toolbar may show: their own, plus shared ones.

    ⚠️ ANOTHER PERSON'S PERSONAL VIEW IS NOT VISIBLE, even though read in this
    module is account-wide. A personal view is a working note — half-built
    filters, "to chase", "mine only" — and putting those on a colleague's
    toolbar is not a feature anybody asked for.
    """
    me = str(user_id or "")
    return [v for v in views if is_shared(v) or owner_of(v) == me]


def may_write(row: Any, *, user_id: Any, is_account_owner: bool = False,
              has_manage_others: bool = False) -> bool:
    """May this person edit or delete this view?"""
    me = str(user_id or "")
    if owner_of(row) == me:
        return True
    if not is_shared(row):
        # Not theirs and not shared: they cannot even see it, let alone write.
        return False
    return bool(is_account_owner or has_manage_others)


def write_refusal(row: Any) -> str:
    """The message for a refused write. Names the reason, not just 'denied'."""
    if is_shared(row):
        return ("That view is shared with the account — only the person who "
                "created it, or an account owner, can change it.")
    return "That view belongs to someone else."


def assert_object_type(value: Any) -> Tuple[str, Optional[str]]:
    kind = str(value or "").strip().lower()
    if kind not in OBJECT_TYPES:
        return "", ("A view is saved for companies, leads or contacts.")
    return kind, None


def name_is_free(name: str, object_type: str, *, views: Sequence[Any],
                 user_id: Any, shared: bool,
                 exclude_id: Any = None) -> Optional[str]:
    """``None`` when the name may be used, else the message.

    ⚠️ UNIQUE WITHIN THE SCOPE THAT DISPLAYS IT, NOT WITHIN THE ACCOUNT. Two
    people may each have a personal "My deals" — they never appear on the same
    toolbar. A SHARED name must not collide with another shared one, nor with
    one of this person's own, because those two do.
    """
    me = str(user_id or "")
    target = name.casefold()
    for view in views:
        if exclude_id is not None and str(_get(view, "id")) == str(exclude_id):
            continue
        if str(_get(view, "object_type") or "") != object_type:
            continue
        if str(_get(view, "name") or "").casefold() != target:
            continue
        # The only pair that never shares a toolbar: two PERSONAL views
        # belonging to different people. Everything else collides.
        both_personal_and_not_mine = (
            not shared and not is_shared(view) and owner_of(view) != me)
        if both_personal_and_not_mine:
            continue
        if is_shared(view) and not shared:
            return (f"There is already a shared view called “{name}”.")
        if shared and not is_shared(view) and owner_of(view) != me:
            return (f"Somebody already has a view called “{name}” — "
                    f"a shared one would be indistinguishable from it.")
        return f"You already have a view called “{name}”."
    return None


def at_capacity(views: Sequence[Any], object_type: str) -> Optional[str]:
    count = sum(1 for v in views
                if str(_get(v, "object_type") or "") == object_type)
    if count >= MAX_VIEWS:
        return (f"That is {MAX_VIEWS} saved views already — delete one before "
                f"saving another.")
    return None


def validate_group(group: Any, object_type: str,
                   attributes: Optional[Sequence[Any]] = None) -> Optional[str]:
    """Refuse a group that could not be run. Write-time only.

    ⚠️ THE SAME COMPILER THE LIST USES. Validating with a second, looser rule
    here is how a view gets saved that the list then refuses — the user names
    it, sees it on the toolbar, clicks it, and gets an error about a filter
    they were told was fine.
    """
    if not group:
        return None
    parsed, error = filter_rules.parse(group)
    if error:
        return error
    _, error = filter_rules.build(parsed, object_type, attributes)
    return error


def stale_fields(group: Any, object_type: str,
                 attributes: Optional[Sequence[Any]] = None) -> List[str]:
    """Field names in this stored group that no longer exist. Read-time.

    REPORTED, NOT PRUNED. Pruning would hand back a view that runs and means
    something different from what was saved — a narrower filter silently
    widened. The toolbar marks it and offers to fix or delete it.
    """
    parsed, error = filter_rules.parse(group)
    if error or not parsed:
        return []
    known = {row["field"] for row
             in filter_rules.field_catalogue(object_type, attributes)}
    out: List[str] = []
    for condition in (_get(parsed, "conditions") or []):
        name = str(_get(condition, "field") or "").strip()
        if name and name not in known and name not in out:
            out.append(name)
    return out


def default_for(views: Sequence[Any], object_type: str, user_id: Any) -> Any:
    """The view this person's list should open on, or None.

    Their OWN default wins over a shared one: an account owner marking a shared
    view as the default is setting the house default, not overriding a choice
    somebody made for themselves.
    """
    me = str(user_id or "")
    mine = [v for v in views
            if str(_get(v, "object_type") or "") == object_type
            and _get(v, "is_default")]
    for view in mine:
        if owner_of(view) == me and not is_shared(view):
            return view
    return mine[0] if mine else None


def ordered(views: Sequence[Any]) -> List[Any]:
    """Toolbar order: shared views first, then personal, each alphabetical.

    Stable and name-based rather than by creation time, so a view does not move
    on the toolbar because somebody else saved one.
    """
    return sorted(
        views,
        key=lambda v: (0 if is_shared(v) else 1,
                       str(_get(v, "name") or "").casefold()),
    )


def payload(view: Any, *, user_id: Any, is_account_owner: bool = False,
            has_manage_others: bool = False,
            attributes: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """One view as the toolbar needs it, including what the caller may do.

    ``can_edit`` ships with the row so the UI does not re-implement the
    ownership rule and offer a button the server will refuse.
    """
    object_type = str(_get(view, "object_type") or "company")
    group = _get(view, "filter_group") or {}
    stale = stale_fields(group, object_type, attributes)
    return {
        "id": _get(view, "id"),
        "name": _get(view, "name"),
        "object_type": object_type,
        "filter_group": group,
        "columns": list(_get(view, "columns") or []),
        "sort_by": _get(view, "sort_by"),
        "sort_dir": _get(view, "sort_dir") or "desc",
        "is_shared": is_shared(view),
        "is_default": bool(_get(view, "is_default")),
        "is_mine": owner_of(view) == str(user_id or ""),
        "can_edit": may_write(view, user_id=user_id,
                              is_account_owner=is_account_owner,
                              has_manage_others=has_manage_others),
        # Names in the stored group that no longer exist. Non-empty means the
        # view will be refused if it is run — the toolbar says so rather than
        # letting the user find out by clicking it.
        "stale_fields": stale,
        "created_by_user_id": _get(view, "created_by_user_id"),
        "created_at": _get(view, "created_at"),
        "updated_at": _get(view, "updated_at"),
    }
