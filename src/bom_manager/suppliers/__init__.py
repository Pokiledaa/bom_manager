"""Supplier integrations for part lookup and pricing."""

from bom_manager.suppliers.base import (
    PartDetail,
    PartNotFoundError,
    PartResult,
    PriceBreakInfo,
    SupplierError,
    SupplierNetworkError,
    SupplierParseError,
    SupplierProtocol,
)
from bom_manager.suppliers.lcsc import BrowserManager, LCSCSupplier

__all__ = [
    "BrowserManager",
    "LCSCSupplier",
    "PartDetail",
    "PartNotFoundError",
    "PartResult",
    "PriceBreakInfo",
    "SupplierError",
    "SupplierNetworkError",
    "SupplierParseError",
    "SupplierProtocol",
]
