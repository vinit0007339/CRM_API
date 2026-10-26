"""Fractional card ordering on the Kanban board. CRM-015.

A deal's place in its column is a float, not an index. Dropping a card between
neighbours A and B sets ``(A.position + B.position) / 2``, so a drag is one
single-document write instead of renumbering every card below it.

Floats do run out of room: repeatedly dropping into the *same* slot halves the
gap each time, and after ~50 consecutive inserts it falls below what a float64
can represent distinctly. ``needs_renormalisation`` detects that and the caller
re-spaces the column back to whole ``STEP`` multiples.

Stdlib-only. No DB, no pydantic.
"""
from __future__ import annotations

from typing import List, Optional

# Spacing between freshly-numbered cards. Large enough that ~50 midpoint
# inserts fit between any two neighbours before renormalisation is needed.
STEP = 1000.0

# Below this gap, the midpoint stops being reliably distinct from its
# neighbours in float64. Renormalise instead.
MIN_GAP = 1e-6


def initial_position() -> float:
    """Position for the first card in an empty column."""
    return STEP


def append_position(last: Optional[float]) -> float:
    """Position for a card appended to the end of a column."""
    if last is None:
        return initial_position()
    return last + STEP


def prepend_position(first: Optional[float]) -> float:
    """Position for a card dropped above every existing card."""
    if first is None:
        return initial_position()
    return first - STEP


def needs_renormalisation(before: Optional[float], after: Optional[float]) -> bool:
    """True when the gap between two neighbours is too small to subdivide."""
    if before is None or after is None:
        return False
    return abs(after - before) < MIN_GAP


def between(before: Optional[float], after: Optional[float]) -> float:
    """Position for a card dropped between two neighbours.

    ``before`` is the card above the drop point, ``after`` the one below.
    Either may be None (top or bottom of the column); both None means the
    column is empty.

    Raises:
        ValueError: the neighbours are out of order, or the gap is exhausted —
            in both cases the caller must renormalise the column and retry.
    """
    if before is None and after is None:
        return initial_position()
    if before is None:
        return prepend_position(after)
    if after is None:
        return append_position(before)
    if after <= before:
        raise ValueError(
            "Neighbour positions are out of order — renormalise the column."
        )
    if needs_renormalisation(before, after):
        raise ValueError(
            "No room between these cards — renormalise the column and retry."
        )
    return (before + after) / 2.0


def renormalise(count: int) -> List[float]:
    """Evenly-spaced positions for a whole column, in display order.

    Apply in the same order as the column's current sort so nothing visibly
    moves for the user.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    return [STEP * (i + 1) for i in range(count)]


def resolve(
    before: Optional[float],
    after: Optional[float],
    *,
    column_size: int = 0,
) -> tuple:
    """Convenience wrapper: ``(position, renormalise_needed)``.

    Lets the route layer handle both outcomes without catching ValueError.
    """
    try:
        return between(before, after), False
    except ValueError:
        positions = renormalise(column_size)
        mid = positions[len(positions) // 2] if positions else initial_position()
        return mid, True
