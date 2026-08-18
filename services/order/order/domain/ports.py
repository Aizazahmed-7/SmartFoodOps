"""Outbound ports — what the order domain needs from the world, as
Protocols the adapters satisfy and tests fake."""

from dataclasses import dataclass
from typing import Any, Protocol

from ..values import (
    AuthResult,
    LineSpec,
    PlacementAck,
    PlacementInput,
    ReserveResult,
    Verdict,
)


@dataclass(frozen=True)
class PlacementPending:
    """The workflow is durably started, but its create_order activity has
    not reported back inside our await budget — slow, backlogged or
    restarting workers.

    This is NOT a failure: Temporal is holding the intent, so the order is
    coming. The customer still gets their 202 (a 5xx here would invite a
    duplicate re-order against a live workflow); the only thing briefly
    suspended is read-your-writes — GET /v1/orders/{id} may 404 for a
    moment, and the idempotency key stays IN_PROGRESS until the activity
    commits, so an immediate retry sees 409 rather than the stored reply."""

    order_id: str


class RestaurantNotFound(Exception):
    pass


class SnapshotUnavailable(Exception):
    """Catalog unreachable after retries — surfaces as 503."""


class AddressNotFound(Exception):
    """Unknown address OR someone else's — identity answers 404 for both."""


class AddressUnavailable(Exception):
    """Identity unreachable after retries — surfaces as 503."""


class SagaGone(Exception):
    """The target workflow already finished — the signal has nobody to
    reach. For a decision this means the window closed (timer or a prior
    verdict); the route maps it, never retries it."""


class SagaUnavailable(Exception):
    """Temporal unreachable — surfaces as 503 (the kitchen retries)."""


class SagaClosed(Exception):
    """ord::{order_id} exists but has FINISHED, so REJECT_DUPLICATE refuses
    to start it again. Only reachable when an idempotency key is reused past
    its replay TTL — the derived order id then points at a settled order.
    The domain answers it from the database, not the adapter."""


class PaymentStateConflict(Exception):
    """Capture refused because there is nothing to capture (no auth, or it
    was voided). Unlike void/refund — where nothing-to-undo IS success —
    a capture that finds no money is a real fault: retrying cannot help,
    so the activity marks it non-retryable and the workflow fails loudly."""


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
    async def place(self, placement: PlacementInput) -> PlacementAck | PlacementPending:
        """Start ord::{order_id} and wait for it to create the order row
        (ADR-0023). One call does both — start and await — because the
        workflow IS the placement now.

        Must be idempotent: the same placement may arrive twice (a client
        retry after the stale-key takeover), and both calls must converge on
        the one workflow rather than fork. Raises SagaUnavailable when
        Temporal cannot be reached — placement's new hard dependency."""
        ...

    async def signal_decision(self, order_id: str, verdict: Verdict) -> None:
        """Deliver the restaurant's verdict to ord::{order_id}. Raises
        SagaGone (workflow finished — window closed) or SagaUnavailable."""
        ...

    async def signal_food_ready(self, order_id: str) -> None:
        """Tell dlv::{order_id} the kitchen is done. Same exceptions."""
        ...

    async def signal_cancel(self, order_id: str) -> None:
        """Deliver the customer's cancel request to ord::{order_id}.
        Same exceptions; the workflow referees whether it is honored."""
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
