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
from .domain.ports import InventoryOpsPort, PaymentOpsPort
from .domain.transitions import IllegalTransition, transition
from .values import (
    ActivityName,
    AuthResult,
    CancelReason,
    LineSpec,
    PriceResult,
    ReserveResult,
)


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

    # ── the unwind (§7 compensation table) ─────────────────────────

    @activity.defn(name=ActivityName.BEGIN_CANCEL)
    async def begin_cancel(self, order_id: str, expected: OrderStatus) -> None:
        """The workflow knows exactly where the order stands (deterministic
        history), so it names the expected state."""
        await self._transition(order_id, expected=expected, target="CANCELLING")

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
            self.begin_cancel,
            self.void_authorization,
            self.release_reservation,
            self.finish_cancel,
        ]
