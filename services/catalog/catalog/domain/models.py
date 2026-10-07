"""Domain models — plain frozen dataclasses, no pydantic, no SQLAlchemy.

The domain speaks these; the API layer converts them to response DTOs and
the adapters build them from rows. Same convention as identity.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Restaurant:
    id: str
    owner_user_id: str
    name: str
    cuisines: list[str]
    lat: float | None
    lon: float | None
    hours: dict[str, Any] | None
    # None on a BRAND, always set on a branch: these live on branch_metadata
    # since 0009, and a brand has no row there. A brand is a menu template,
    # not a place — it has no address, no schedule and nothing to pause.
    city: str | None
    status: str | None  # open | paused
    timezone: str | None
    kind: str = "branch"  # brand | branch (ADR-0028)
    brand_id: str | None = None  # parent brand for branches; None for brands
    branch_label: str | None = None  # "Downtown" — unique within the brand

    @property
    def display_name(self) -> str:
        """What customers and couriers see: 'Biryani House — Downtown'.
        `name` stays the pure brand name so find-by-name keeps working."""
        return f"{self.name} — {self.branch_label}" if self.branch_label else self.name
