"""View-preference request schemas. CRM-248."""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class ViewPreferenceUpdateRequest(BaseModel):
    """A whole layout, in order.

    ``columns`` carries attribute ``internal_name``s. Deliberately typed as a
    plain list of strings rather than an enum: the valid set is per-account
    (custom attributes) and changes at runtime, so validation belongs in
    ``column_rules.validate`` against that account's definitions — where the
    error message can name the column and the object type.
    """

    object_type: str = Field("company", pattern="^(company|deal|contact)$")
    columns: List[str] = Field(default_factory=list)
