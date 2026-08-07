"""Pydantic models for Dunkin' CRM data."""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "CustomerFavoriteItem",
    "CustomerSuggestion",
    "CustomerProfile",
]


class CustomerFavoriteItem(BaseModel):
    """Represents a frequent item for a known customer."""

    item: str
    size: str | None = None
    quantity: int = 1
    price: float | None = None


class CustomerSuggestion(BaseModel):
    """Suggested upsell or personalized bundle."""

    headline: str
    description: str | None = None
    items: list[CustomerFavoriteItem] = Field(default_factory=list)


class CustomerProfile(BaseModel):
    """Full CRM profile surfaced to the voice and dashboard experiences."""

    id: str
    name: str
    rewards_status: str
    loyalty_score: int = 0
    loyalty_goal: int = 1000
    curbside_preferred: bool = False
    bluetooth_devices: list[str] = Field(default_factory=list, description="Known Bluetooth MAC addresses")
    favorite_items: list[CustomerFavoriteItem] = Field(default_factory=list)
    usual_order: list[CustomerFavoriteItem] = Field(default_factory=list)
    suggested_sales: list[str] = Field(default_factory=list)
    suggestions: list[CustomerSuggestion] = Field(default_factory=list)
    last_visit_iso: str | None = None

    @property
    def loyalty_progress_pct(self) -> float:
        if self.loyalty_goal <= 0:
            return 0.0
        return max(0.0, min(1.0, self.loyalty_score / self.loyalty_goal))
