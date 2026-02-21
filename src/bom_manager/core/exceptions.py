"""Domain exceptions for the BOM Manager service layer."""

from __future__ import annotations


class BOMManagerError(Exception):
    """Base class for all BOM Manager application errors."""


class ProjectNotFoundError(BOMManagerError):
    """Raised when a project cannot be found by ID or name."""


class VersionNotFoundError(BOMManagerError):
    """Raised when a project version cannot be found."""


class ItemNotFoundError(BOMManagerError):
    """Raised when a BOM item cannot be found within a version."""


class SupplierLookupError(BOMManagerError):
    """Raised when a supplier search returns no usable results."""


class ExportError(BOMManagerError):
    """Raised when a BOM export cannot be completed."""
