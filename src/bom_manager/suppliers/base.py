"""Abstract supplier protocol and shared result models."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PartResult(BaseModel):
    """Lightweight search result — enough to pick the right part."""

    model_config = ConfigDict(frozen=True)

    mpn: str = Field(..., description="Manufacturer Part Number")
    supplier_pn: str = Field(..., description="Supplier's own catalogue number")
    description: str = Field(default="")
    manufacturer: str = Field(default="")
    url: str = Field(default="", description="Product page URL on the supplier's site")


class PartDetail(PartResult):
    """Full part detail including pricing and availability."""

    model_config = ConfigDict(frozen=True)

    price_breaks: list[PriceBreakInfo] = Field(default_factory=list)
    stock: int = Field(default=0, ge=0)
    datasheet_url: Optional[str] = Field(default=None)
    currency: str = Field(default="USD", description="ISO currency code for prices (USD or IRR)")

    def best_unit_price(self, quantity: int = 1) -> Optional[Decimal]:
        """Return the lowest unit price for the given order quantity."""
        eligible = [pb for pb in self.price_breaks if pb.min_quantity <= quantity]
        if not eligible:
            return self.price_breaks[0].unit_price if self.price_breaks else None
        return min(eligible, key=lambda pb: pb.unit_price).unit_price


class PriceBreakInfo(BaseModel):
    """Price tier returned by a supplier."""

    model_config = ConfigDict(frozen=True)

    min_quantity: int = Field(..., gt=0)
    unit_price: Decimal = Field(..., ge=0)


# PartDetail references PriceBreakInfo, so rebuild after both are defined
PartDetail.model_rebuild()


class SupplierError(Exception):
    """Base exception for all supplier-related errors."""


class PartNotFoundError(SupplierError):
    """Raised when a part lookup returns no results."""


class SupplierNetworkError(SupplierError):
    """Raised on HTTP / connectivity failures."""


class SupplierParseError(SupplierError):
    """Raised when the supplier response cannot be parsed."""


@runtime_checkable
class SupplierProtocol(Protocol):
    """Structural protocol every supplier adapter must satisfy."""

    #: Human-readable name used in logging and storage keys
    name: str

    def search(self, query: str) -> list[PartResult]:
        """
        Search for parts matching *query*.

        Returns an empty list (not an exception) when nothing matches.
        May raise SupplierError subclasses on infrastructure failures.
        """
        ...

    def get_part(self, part_number: str) -> PartDetail:
        """
        Fetch full detail for a single supplier part number.

        Raises PartNotFoundError if the part does not exist.
        May raise SupplierError subclasses on infrastructure failures.
        """
        ...
