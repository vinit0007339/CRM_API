"""Import-run CRUD. CRM-229."""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.models.import_run import CrmImportModel

from app.services.queries import assert_account_scoped

from .base import CrmCrudBase


class CrmImportCrud(CrmCrudBase):
    model = CrmImportModel

    def build_filter(self, *, account_id: Any,
                     entity_type: Optional[str] = None,
                     status: Optional[str] = None) -> Dict[str, Any]:
        """ONE builder for the list and its count, so they cannot drift."""
        flt: Dict[str, Any] = {"created_by": self.oid(account_id)}
        if entity_type:
            flt["entity_type"] = entity_type
        if status:
            flt["status"] = status
        return assert_account_scoped(flt)
