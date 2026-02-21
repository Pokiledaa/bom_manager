"""Core domain models and exceptions.

Services (BOMService, ProjectService) are importable from their own modules
to avoid circular imports with the storage layer:

    from bom_manager.core.bom_service import BOMService
    from bom_manager.core.project_service import ProjectService
"""

from bom_manager.core.exceptions import (
    BOMManagerError,
    ExportError,
    ItemNotFoundError,
    ProjectNotFoundError,
    SupplierLookupError,
    VersionNotFoundError,
)
from bom_manager.core.models import (
    BOMItem,
    BOMSummary,
    PriceBreak,
    Project,
    ProjectVersion,
)

__all__ = [
    # Exceptions
    "BOMManagerError",
    "ExportError",
    "ItemNotFoundError",
    "ProjectNotFoundError",
    "SupplierLookupError",
    "VersionNotFoundError",
    # Models
    "BOMItem",
    "BOMSummary",
    "PriceBreak",
    "Project",
    "ProjectVersion",
]
