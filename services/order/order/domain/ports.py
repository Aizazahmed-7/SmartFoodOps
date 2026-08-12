"""Outbound ports — what the order domain needs from the world, as
Protocols the adapters satisfy and tests fake."""

from typing import Any, Protocol

from ..values import AuthResult, LineSpec, ReserveResult


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


class InventoryOpsPort(Protocol):
    """The saga's stock operations (worker activities). Structural, so
    tests fake it without touching HTTP."""

    async def reserve(
        self, *, order_id: str, restaurant_id: str, lines: list[LineSpec]
    ) -> ReserveResult: ...

    async def release(self, order_id: str, *, reason: str = "cancelled") -> None: ...

    async def commit(self, order_id: str) -> None: ...


class PaymentOpsPort(Protocol):
    """The saga's money operations (worker activities)."""

    async def authorize(
        self, order_id: str, *, amount_cents: int, currency: str, card_token: str
    ) -> AuthResult: ...

    async def void(self, order_id: str) -> None: ...

    async def capture(self, order_id: str) -> None: ...

    async def refund(self, order_id: str) -> None: ...
