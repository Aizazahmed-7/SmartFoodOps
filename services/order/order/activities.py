"""The saga's activities — where the workflow's decisions touch the world.

Every activity is at-least-once + idempotent (the architecture's consumer
rule, applied to Temporal): reservation PK = order_id, money keys =
{order_id}:{op}, transitions guarded by expected-status. A retried activity
therefore replays instead of repeating.

Business outcomes return as VALUES ("declined", "item_unavailable") so the
workflow's control flow stays deterministic; only transport failures raise
(and get retried by policy). IllegalTransition is marked non-retryable —
retrying an illegal state move can never make it legal; it means the world
and the workflow's history disagree, which is a page, not a retry.
"""

from smartfood_kafka import EventType
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .adapters.repo import OrderRepo
from .db import OrderStatus
from .domain.ports import InventoryOpsPort, PaymentOpsPort, PaymentStateConflict
from .domain.transitions import IllegalTransition, begin_cancel_from, transition
from .values import (
    ActivityName,
    AuthResult,
    CancelOutcome,
    CancelReason,
    LineSpec,
    PriceResult,
    ReserveResult,
)

# What a customer cancel may interrupt mid-kitchen. PICKED_UP is absent on
# purpose: once the courier holds the food, the cancel loses (FR-21).
CANCELLABLE_KITCHEN_STATES: tuple[OrderStatus, ...] = ("ACCEPTED", "PREPARING", "READY")


class OrderActivities:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        inventory: InventoryOpsPort,
        payment: PaymentOpsPort,
    ):
        self._sessions = sessions
        self._inventory = inventory
        self._payment = payment

    # ── the forward path ───────────────────────────────────────────

    @activity.defn(name=ActivityName.PRICE_ORDER)
    async def price_order(self, order_id: str) -> PriceResult:
        """LOCAL activity: load the immutable placement snapshot (flag #2 —
        never recomputed). Pure read; one query pass."""
        async with self._sessions() as session:
            repo = OrderRepo(session)
            order = await repo.get_order_any(order_id)
            if order is None:
                raise ApplicationError(f"unknown order {order_id}", non_retryable=True)
            items = await repo.get_items(order_id)
        return PriceResult(
            restaurant_id=order.restaurant_id,
            amount_cents=order.pricing_snapshot["total_cents"],
            currency=order.pricing_snapshot["currency"],
            card_token=order.card_token,
            lines=[LineSpec(item_id=i.menu_item_id, qty=i.qty) for i in items],
        )

    @activity.defn(name=ActivityName.VALIDATE_AND_RESERVE)
    async def validate_and_reserve(self, order_id: str, price: PriceResult) -> ReserveResult:
        outcome = await self._inventory.reserve(
            order_id=order_id, restaurant_id=price.restaurant_id, lines=price.lines
        )
        if outcome == "ok":
            await self._transition(order_id, expected="PLACED", target="VALIDATED")
        return outcome

    @activity.defn(name=ActivityName.AUTHORIZE_PAYMENT)
    async def authorize_payment(self, order_id: str, price: PriceResult) -> AuthResult:
        outcome = await self._payment.authorize(
            order_id,
            amount_cents=price.amount_cents,
            currency=price.currency,
            card_token=price.card_token,
        )
        if outcome == "ok":
            await self._transition(order_id, expected="VALIDATED", target="PAYMENT_CLEARED")
        return outcome

    @activity.defn(name=ActivityName.CONFIRM_ORDER)
    async def confirm_order(self, order_id: str) -> None:
        await self._transition(
            order_id,
            expected="PAYMENT_CLEARED",
            target="CONFIRMED",
            event=EventType.ORDER_CONFIRMED,
        )

    @activity.defn(name=ActivityName.MARK_ACCEPTED)
    async def mark_accepted(self, order_id: str) -> None:
        await self._transition(order_id, expected="CONFIRMED", target="ACCEPTED")

    # ── delivery + settlement (S6) ─────────────────────────────────

    @activity.defn(name=ActivityName.MARK_PICKED_UP)
    async def mark_picked_up(self, order_id: str) -> None:
        await self._transition(order_id, expected="READY", target="PICKED_UP")

    @activity.defn(name=ActivityName.MARK_DELIVERED)
    async def mark_delivered(self, order_id: str) -> None:
        await self._transition(
            order_id,
            expected="PICKED_UP",
            target="DELIVERED",
            event=EventType.ORDER_DELIVERED,
        )

    @activity.defn(name=ActivityName.CAPTURE_PAYMENT)
    async def capture_payment(self, order_id: str) -> None:
        """Take the held funds ({order_id}:capture money key inside payment).
        Nothing-to-capture is NOT convergent — settling without money would
        ship food for free, so it fails the workflow loudly (a page)."""
        try:
            await self._payment.capture(order_id)
        except PaymentStateConflict as exc:
            raise ApplicationError(
                str(exc), non_retryable=True, type="PaymentStateConflict"
            ) from None

    @activity.defn(name=ActivityName.SETTLE_ORDER)
    async def settle_order(self, order_id: str) -> None:
        """Consume the reservation (stock leaves the building for good),
        then close the order. Both halves replay clean: commit on a
        consumed reservation is a no-op, the transition is guarded."""
        await self._inventory.commit(order_id)
        await self._transition(
            order_id,
            expected="DELIVERED",
            target="SETTLED",
            event=EventType.ORDER_SETTLED,
        )

    # ── the unwind (§7 compensation table) ─────────────────────────

    @activity.defn(name=ActivityName.BEGIN_CANCEL)
    async def begin_cancel(
        self, order_id: str, expected: OrderStatus, reason: CancelReason
    ) -> None:
        """The workflow knows exactly where the order stands (deterministic
        history), so it names the expected state — and stamps the reason NOW,
        not at finish: the unwind can hold CANCELLING for an unbounded window
        (compensations retry forever), and the kitchen's decision matrix
        needs the reason to classify replies monotonically during it."""
        await self._transition(
            order_id, expected=expected, target="CANCELLING", cancel_reason=reason
        )

    @activity.defn(name=ActivityName.TRY_BEGIN_CANCEL)
    async def try_begin_cancel(self, order_id: str, reason: CancelReason) -> CancelOutcome:
        """The customer-vs-courier referee: set-guarded because the workflow
        cannot know which kitchen state the row is in when the cancel lands.
        too_late is a VALUE — losing the race is an answer, not an error."""
        cancelled = await begin_cancel_from(
            self._sessions, order_id, allowed=CANCELLABLE_KITCHEN_STATES, reason=reason
        )
        return "ok" if cancelled else "too_late"

    @activity.defn(name=ActivityName.VOID_AUTHORIZATION)
    async def void_authorization(self, order_id: str) -> None:
        await self._payment.void(order_id)  # 409 no-auth converges inside the client

    @activity.defn(name=ActivityName.RELEASE_RESERVATION)
    async def release_reservation(self, order_id: str) -> None:
        await self._inventory.release(order_id)  # not-active = idempotent no-op

    @activity.defn(name=ActivityName.FINISH_CANCEL)
    async def finish_cancel(self, order_id: str, reason: CancelReason) -> None:
        await self._transition(
            order_id,
            expected="CANCELLING",
            target="CANCELLED",
            event=EventType.ORDER_CANCELLED,
            cancel_reason=reason,
        )

    # ── helpers ────────────────────────────────────────────────────

    async def _transition(
        self,
        order_id: str,
        *,
        expected: OrderStatus,
        target: OrderStatus,
        event: EventType | None = None,
        cancel_reason: str | None = None,
    ) -> None:
        try:
            await transition(
                self._sessions,
                order_id,
                expected=expected,
                target=target,
                event=event,
                cancel_reason=cancel_reason,
            )
        except IllegalTransition as exc:
            raise ApplicationError(str(exc), non_retryable=True, type="IllegalTransition") from None

    def all(self) -> list:
        """Everything the worker registers."""
        return [
            self.price_order,
            self.validate_and_reserve,
            self.authorize_payment,
            self.confirm_order,
            self.mark_accepted,
            self.mark_picked_up,
            self.mark_delivered,
            self.capture_payment,
            self.settle_order,
            self.begin_cancel,
            self.try_begin_cancel,
            self.void_authorization,
            self.release_reservation,
            self.finish_cancel,
        ]
