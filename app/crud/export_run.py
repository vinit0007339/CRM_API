"""Export-job CRUD. CRM-235."""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.models.export_run import CrmExportModel

from app.services.queries import assert_account_scoped

from .base import CrmCrudBase


class CrmExportCrud(CrmCrudBase):
    model = CrmExportModel

    def build_filter(self, *, account_id: Any,
                     entity_type: Optional[str] = None) -> Dict[str, Any]:
        """ONE builder for the list and its count, so they cannot drift."""
        flt: Dict[str, Any] = {"created_by": self.oid(account_id)}
        if entity_type:
            flt["entity_type"] = entity_type
        return assert_account_scoped(flt)
