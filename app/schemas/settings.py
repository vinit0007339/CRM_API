"""CRM settings schemas. CRM-243.

Only the fields with a screen and tests behind them are writable. See
``api/routes/crm/settings.py`` for why the write surface is deliberately narrow.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.object_id import PyObjectId


class LostReasonInput(BaseModel):
    """One row of the lost-reason picker, as the settings screen sends it.

    ``id`` identifies an EXISTING reason. Send it back and the reason keeps its
    identity through a rename; omit it and a new one is created. Deals store
    only this id, so dropping it on a rename would orphan every deal closed
    against that reason.

    ``order`` is deliberately absent: the server renumbers from list order, so
    the client cannot send a broken sequence.
    """

    id: Optional[PyObjectId] = None
    label: str = Field(..., max_length=200)
    is_active: bool = True


class CrmSettingsUpdateRequest(BaseModel):
    """Every field optional; absent means unchanged.

    Distinguished with ``model_fields_set`` rather than by treating None as
    "no change", so that clearing the blocklist is expressible.
    """

    auto_link_contacts_by_domain: Optional[bool] = Field(
        None,
        description="Link new contacts to a company matching their email "
                    "domain. Applies to contacts created or edited AFTER this "
                    "is turned on — it does not link existing ones.",
    )
    lost_reasons: Optional[List[LostReasonInput]] = Field(
        None, max_length=200,
        description="The lost-reason picker, in display order. A stored reason "
                    "you leave out is RETIRED, not deleted — a deal closed "
                    "against it still has to render its label, and the id is "
                    "all it kept.",
    )
    card_display_attributes: Optional[List[str]] = Field(
        None, max_length=50,
        description="Which attributes a Kanban card shows, IN ORDER, by "
                    "internal_name. An empty list clears the explicit order "
                    "and hands control back to each attribute's show_on_card. "
                    "An unknown or duplicated name is refused, not dropped — "
                    "a typo would otherwise produce a card missing a field "
                    "with nothing saying why.",
    )
    card_show_next_activity: Optional[bool] = Field(
        None, description="Show the next scheduled activity on a deal card.",
    )
    free_email_domains_blocklist: Optional[List[str]] = Field(
        None, max_length=2000,
        description="Extra domains never treated as a company. ADDS to the "
                    "built-in list of ~200 free providers; it cannot unblock "
                    "one, because an emptied list would otherwise produce a "
                    "single company called Gmail holding every contact.",
    )
