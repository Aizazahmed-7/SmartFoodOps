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

from datetime import datetime

from smartfood_kafka import EventType
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import tracking
from .adapters.repo import OrderRepo
from .db import OrderStatus
from .domain.ports import DispatchPort, InventoryOpsPort, PaymentOpsPort, PaymentStateConflict
from .domain.transitions import (
    IllegalTransition,
    begin_cancel_from,
    record_rider,
    transition,
)
from .metrics import SAGA_OUTCOMES
from .values import (
    ActivityName,
    AuthResult,
    CancelOutcome,
    CancelReason,
    PlacementInput,
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
        dispatch: DispatchPort,
    ):
        self._sessions = sessions
        self._inventory = inventory
        self._payment = payment
        self._dispatch = dispatch

    # ── the forward path ───────────────────────────────────────────

    @activity.defn(name=ActivityName.CREATE_ORDER)
    async def create_order(self, placement: PlacementInput) -> str:
        """The saga's first act (ADR-0023): order row + line snapshots +
        OrderPlaced outbox row, committing together. The row carries
        request_hash — since ADR-0024 the orders row IS the idempotency
        record, so this one transaction is the whole placement fact.

        At-least-once applies here like everywhere else: an activity that
        commits and then loses its worker gets retried. That is survivable
        only because the order id is DERIVED from the idempotency key, so a
        retry aims at the same primary key and the duplicate insert is a
        caught IntegrityError rather than a second order.

        Returns the row's current status — that string is what the waiting
        HTTP request answers with, so it must be the truth from the DB and
        not an assumption from the workflow."""
        if await self._insert_placement(placement):
            return "PLACED"
        return await self._adopt_existing(placement)

    async def _insert_placement(self, placement: PlacementInput) -> bool:
        """One transaction, three writes. False = the row already existed
        (a retry of a commit whose acknowledgement was lost)."""
        now = datetime.fromisoformat(placement.placed_at)
        lines = [
            {
                "menu_item_id": line.menu_item_id,
                "name": line.name,
                "unit_price_cents": line.unit_price_cents,
                "qty": line.qty,
                "options": line.options,
                "line_total_cents": line.line_total_cents,
            }
            for line in placement.lines
        ]
        async with self._sessions() as session:
            repo = OrderRepo(session)
            try:
                await repo.insert_order(
                    order_id=placement.order_id,
                    user_id=placement.user_id,
                    restaurant_id=placement.restaurant_id,
                    restaurant_name=placement.restaurant_name,
                    card_token=placement.card_token,
                    request_hash=placement.request_hash,
                    menu_version=placement.menu_version,
                    pricing_snapshot=placement.pricing_snapshot,
                    address_snapshot=placement.address_snapshot,
                    lines=lines,
                    now=now,
                )
                await repo.stage_event(
                    order_id=placement.order_id,
                    version=0,
                    event_type=EventType.ORDER_PLACED,
                    payload={
                        "order_id": placement.order_id,
                        "user_id": placement.user_id,
                        "restaurant_id": placement.restaurant_id,
                        "restaurant_name": placement.restaurant_name,
                        "status": "PLACED",
                        "menu_version": placement.menu_version,
                        "items": lines,
                        "totals": placement.pricing_snapshot,
                        "delivery_address": placement.address_snapshot,
                        "placed_at": placement.placed_at,
                    },
                    now=now,
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        await tracking.publish_status(placement.order_id, "PLACED")
        return True

    async def _adopt_existing(self, placement: PlacementInput) -> str:
        """The insert lost to an earlier execution of THIS activity. The
        order is already there and, since ADR-0024, the row IS the whole
        record — nothing else is owed. Report its real status."""
        async with self._sessions() as session:
            row = await OrderRepo(session).get_order_any(placement.order_id)
        assert row is not None  # the conflict we caught proves it exists
        return str(row.status)

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
        SAGA_OUTCOMES.labels(outcome="settled", reason="").inc()

    # ── dispatch (the cascade's steps, S-dispatch) ─────────────────

    @activity.defn(name=ActivityName.FIND_AND_OFFER)
    async def find_and_offer(self, order_id: str, attempt: int, exclude: list[str]) -> dict:
        """One cascade step. The workflow sends only ids and counters; THIS
        is where the world's data joins — the order row (dropoff address,
        names) is read here because the worker owns order_db, and the
        pickup pin comes from catalog inside the client. Idempotent by
        dispatch's own guards: a retried offer for a locked rider simply
        falls through to the next candidate."""
        async with self._sessions() as session:
            row = await OrderRepo(session).get_order_any(order_id)
        assert row is not None  # the workflow that created it is calling
        address = row.delivery_address_snapshot or {}
        lat, lon = address.get("lat"), address.get("lon")
        if lat is None or lon is None:
            # Pre-toy-city rows (no coords) limp to the city center rather
            # than wedging the cascade — mirrors the pickup-pin fallback.
            from .adapters.dispatch_client import FALLBACK_PICKUP

            lat, lon = FALLBACK_PICKUP
        return await self._dispatch.find_and_offer(
            order_id,
            user_id=row.user_id,
            restaurant_id=row.restaurant_id,
            restaurant_name=row.restaurant_name_snapshot,
            dropoff=(float(lat), float(lon)),
            attempt=attempt,
            exclude=exclude,
        )

    @activity.defn(name=ActivityName.EXPIRE_OFFER)
    async def expire_offer(self, order_id: str, offer_id: str, rider_id: str) -> dict:
        """The cascade timer fired. Dispatch answers revoked, or
        already_assigned — the lost-accept-signal self-heal."""
        return await self._dispatch.expire_offer(order_id, offer_id=offer_id, rider_id=rider_id)

    @activity.defn(name=ActivityName.UNASSIGN_STALLED)
    async def unassign_stalled(self, order_id: str, rider_id: str) -> dict:
        """FR-30's pickup deadline. Conditional on the rider still owning
        an un-picked-up job — a completed pickup wins (ADR-0011)."""
        return await self._dispatch.unassign_stalled(order_id, rider_id=rider_id)

    @activity.defn(name=ActivityName.CANCEL_DISPATCH)
    async def cancel_dispatch(self, order_id: str) -> dict:
        """The order died while dispatch held something — free it. Runs in
        the child's cancellation cleanup and the no-rider unwind; replays
        converge (a cancelled delivery answers kept/cancelled alike)."""
        return await self._dispatch.cancel(order_id)

    @activity.defn(name=ActivityName.RECORD_RIDER)
    async def record_rider(self, order_id: str, rider_id: str) -> None:
        """Stamp the courier onto the order row: every full-state event
        from here on carries rider_id (analytics' per-rider spans)."""
        await record_rider(self._sessions, order_id, rider_id)

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
        SAGA_OUTCOMES.labels(outcome="cancelled", reason=str(reason)).inc()

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
            self.create_order,
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
            self.find_and_offer,
            self.expire_offer,
            self.unassign_stalled,
            self.cancel_dispatch,
            self.record_rider,
        ]
