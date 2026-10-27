"""CRM CRUD classes.

Injected into routes with the repo's standard dependency:

    crud: CompanyCrud = Depends(get_crud(CompanyCrud))
"""
from .activity import CrmActivityCrud
from .association import CrmAssociationCrud
from .base import CrmCrudBase
from .company import CompanyCrud
from .contact import CrmContactCrud
from .deal import DealCrud
from .file import CrmFileCrud
from .export_run import CrmExportCrud
from .import_run import CrmImportCrud
from .note import CrmNoteCrud
from .pipeline import PipelineCrud
from .task import CrmTaskCrud
from .settings import CrmAttributeCrud, CrmSettingsCrud
from .saved_view import CrmSavedViewCrud
from .view_preference import CrmViewPreferenceCrud

__all__ = [
    "CrmCrudBase", "CompanyCrud", "PipelineCrud", "DealCrud",
    "CrmAssociationCrud", "CrmActivityCrud", "CrmSettingsCrud", "CrmAttributeCrud",
    "CrmNoteCrud", "CrmFileCrud", "CrmTaskCrud", "CrmImportCrud", "CrmExportCrud",
    "CrmViewPreferenceCrud", "CrmSavedViewCrud",
    "CrmContactCrud",
]
