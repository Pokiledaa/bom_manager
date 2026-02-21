"""Core Pydantic models for BOM Manager."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PriceBreak(BaseModel):
    """Price break tier: unit price drops at a minimum quantity threshold."""

    model_config = ConfigDict(frozen=True)

    min_quantity: int = Field(..., gt=0, description="Minimum order quantity for this price tier")
    unit_price: Decimal = Field(..., ge=0, description="Unit price at this quantity tier")


class Project(BaseModel):
    """Top-level project container."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def model_post_init(self, __context: object) -> None:
        # Ensure updated_at >= created_at on construction
        if self.updated_at < self.created_at:
            object.__setattr__(self, "updated_at", self.created_at)


class ProjectVersion(BaseModel):
    """A named snapshot/revision of a project's BOM."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    version_name: str = Field(..., min_length=1, max_length=100)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class BOMItem(BaseModel):
    """A single line item in a Bill of Materials."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    version_id: UUID
    reference_designator: str = Field(
        ...,
        min_length=1,
        description="PCB reference designator, e.g. 'R1', 'C3,C4'",
    )
    user_part_name: str = Field(
        ...,
        min_length=1,
        description="Human-readable part name/description from the user",
    )
    matched_mpn: Optional[str] = Field(
        default=None,
        description="Manufacturer Part Number matched by supplier lookup",
    )
    supplier: Optional[str] = Field(default=None, description="Supplier name, e.g. 'Mouser', 'Digikey'")
    supplier_part_number: Optional[str] = Field(default=None)
    supplier_url: Optional[str] = Field(default=None)
    quantity: int = Field(..., gt=0, description="Number of units required")
    unit_price: Optional[Decimal] = Field(default=None, ge=0)
    price_breaks: list[PriceBreak] = Field(default_factory=list)
    total_price: Optional[Decimal] = Field(default=None, ge=0)

    def effective_unit_price(self) -> Optional[Decimal]:
        """Return the best unit price for the current quantity from price breaks."""
        if not self.price_breaks:
            return self.unit_price
        eligible = [pb for pb in self.price_breaks if pb.min_quantity <= self.quantity]
        if not eligible:
            return self.unit_price
        return min(eligible, key=lambda pb: pb.unit_price).unit_price

    def calculate_total(self) -> Optional[Decimal]:
        """Return quantity × effective unit price, or None if price is unknown."""
        price = self.effective_unit_price()
        if price is None:
            return None
        return Decimal(self.quantity) * price


class BOMSummary(BaseModel):
    """Aggregated summary of all items in a BOM version."""

    model_config = ConfigDict(populate_by_name=True)

    version_id: UUID
    items: list[BOMItem] = Field(default_factory=list)
    total_cost: Decimal = Field(default=Decimal("0"))
    item_count: int = Field(default=0)

    @classmethod
    def from_items(cls, version_id: UUID, items: list[BOMItem]) -> "BOMSummary":
        """Build a BOMSummary by computing totals from a list of BOMItems."""
        total = Decimal("0")
        for item in items:
            line_total = item.calculate_total()
            if line_total is not None:
                total += line_total
        return cls(
            version_id=version_id,
            items=items,
            total_cost=total,
            item_count=len(items),
        )
