"""Outbound ports — what the order domain needs from the world, as
Protocols the adapters satisfy and tests fake."""

from typing import Any, Protocol


class RestaurantNotFound(Exception):
    pass


class SnapshotUnavailable(Exception):
    """Catalog unreachable after retries — surfaces as 503."""


class AddressNotFound(Exception):
    """Unknown address OR someone else's — identity answers 404 for both."""


class AddressUnavailable(Exception):
    """Identity unreachable after retries — surfaces as 503."""


class CatalogPort(Protocol):
    async def get_snapshot(self, restaurant_id: str, item_ids: list[str]) -> dict[str, Any]:
        """Catalog's authoritative pricing read. Raises RestaurantNotFound
        (unknown restaurant) or SnapshotUnavailable (catalog down)."""
        ...


class IdentityPort(Protocol):
    async def get_address(self, user_id: str, address_id: str) -> dict[str, Any]:
        """Server-side delivery-address resolution. Raises AddressNotFound
        or AddressUnavailable."""
        ...


class SagaPort(Protocol):
    async def start(self, order_id: str) -> None:
        """Kick the order workflow. S3 stub logs; S5 starts Temporal.
        Runs AFTER the placement commit (never network inside the tx) —
        the commit→start gap is the accepted W3 sweeper exposure."""
        ...
