"""Outbound ports — what the order domain needs from the world, as
Protocols the adapters satisfy and tests fake."""

from typing import Any, Protocol


class RestaurantNotFound(Exception):
    pass


class SnapshotUnavailable(Exception):
    """Catalog unreachable after retries — surfaces as 503."""


class CatalogPort(Protocol):
    async def get_snapshot(self, restaurant_id: str, item_ids: list[str]) -> dict[str, Any]:
        """Catalog's authoritative pricing read. Raises RestaurantNotFound
        (unknown restaurant) or SnapshotUnavailable (catalog down)."""
        ...
