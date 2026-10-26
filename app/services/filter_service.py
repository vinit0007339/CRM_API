"""The advanced filter builder, with the account's attributes fetched. E22.

``filter_rules`` is deliberately pure — plain dicts in, a Mongo fragment out —
so every rule in it is unit-testable without a database. This is the thin
layer that reads the account's attribute definitions and hands them over, in
the same shape as ``bulk_rules`` / ``bulk_service`` and
``lost_reason_rules`` / the settings route.

⚠️ THE ATTRIBUTE FETCH IS NOT OPTIONAL AND ITS FAILURE IS NOT SWALLOWED. The
catalogue is what decides whether a field exists; an empty catalogue would
make every condition on a custom attribute look like a typo and refuse a
filter the user built correctly. A database failure here belongs in the 500,
not hidden behind a confusing message.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.services import filter_rules


async def _attribute_rows(attributes_crud: Any, account_id: Any,
                          object_type: str) -> List[Any]:
    return await attributes_crud.list(account_id=account_id,
                                      object_type=object_type)


async def fragment_for(attributes_crud: Any, account_id: Any, object_type: str,
                       raw: Any, context: Optional[Dict[str, Any]] = None
                       ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(fragment, error)`` for a list endpoint's ``filters`` parameter.

    ``({}, None)`` means "no filter was sent" and merges harmlessly. An error
    is user-facing and the route answers it as a business failure, never as an
    unfiltered list.

    ``context`` carries what a derived field needs that is not on the record.
    Today that is one thing: ``{"stages": [...]}`` for the deal ``is_rotting``
    filter, whose answer depends on each stage's ``rotting_days`` (CRM-411).
    Omitting it does not silently drop the condition — `filter_rules` refuses it
    with a message telling the user to pick a pipeline.
    """
    group, error = filter_rules.parse(raw)
    if error:
        return None, error
    if not group:
        return {}, None
    rows = await _attribute_rows(attributes_crud, account_id, object_type)
    return filter_rules.build(group, object_type, rows, context)


async def catalogue_for(attributes_crud: Any, account_id: Any,
                        object_type: str) -> List[Dict[str, Any]]:
    """Every filterable field for this object — the UI's whole vocabulary.

    The builder renders from this and nothing else, so a user cannot compose a
    condition the server would then refuse.
    """
    rows = await _attribute_rows(attributes_crud, account_id, object_type)
    return filter_rules.field_catalogue(object_type, rows)
