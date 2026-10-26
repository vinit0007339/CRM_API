"""Saved-view request and response schemas. E23."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.object_id import PyObjectId


class SavedViewCreateRequest(BaseModel):
    name: str
    object_type: str = "company"
    # The GROUP, not a compiled fragment — see the model docstring.
    filter_group: Dict[str, Any] = Field(default_factory=dict)
    columns: List[str] = Field(default_factory=list)
    sort_by: Optional[str] = None
    sort_dir: str = "desc"
    is_shared: bool = False
    is_default: bool = False


class SavedViewUpdateRequest(BaseModel):
    """PATCH: every field optional, and ABSENT MEANS UNCHANGED.

    ⚠️ `None` IS NOT "UNCHANGED" FOR `sort_by`. Clearing a view's sort is a
    real edit, so the route distinguishes the two with
    `model_dump(exclude_unset=True)` rather than by testing for None — the
    mistake that makes "remove the sort" silently do nothing.
    """

    name: Optional[str] = None
    filter_group: Optional[Dict[str, Any]] = None
    columns: Optional[List[str]] = None
    sort_by: Optional[str] = None
    sort_dir: Optional[str] = None
    is_shared: Optional[bool] = None
    is_default: Optional[bool] = None


class SavedViewDetails(BaseModel):
    id: Optional[PyObjectId] = None
    name: str = ""
    object_type: str = "company"
    filter_group: Dict[str, Any] = Field(default_factory=dict)
    columns: List[str] = Field(default_factory=list)
    sort_by: Optional[str] = None
    sort_dir: str = "desc"
    is_shared: bool = False
    is_default: bool = False
    is_mine: bool = False
    can_edit: bool = False
    # Field names in the stored group that no longer exist. Non-empty means
    # running this view would be refused, so the toolbar marks it.
    stale_fields: List[str] = Field(default_factory=list)
    created_by_user_id: Optional[PyObjectId] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
