"""Kitchen operations — the restaurant's side of the order lifecycle (S6).

Two kinds of move live here, and the split is the design:

- accept/reject are DECISIONS: the workflow owns that fork (it is parked at
  the §6 durable wait), so the kitchen's verdict travels as a SIGNAL and
  the API answers 202 — "submitted", not "applied". The status catches up
  when the saga moves.
- preparing/ready are FACTS about the kitchen: nobody is waiting on a
  fork, so they go straight through the guarded transition() helper (flag
  #3: every status write, workflow or API, uses the same referee). /ready
  additionally signals the DeliveryWorkflow — the courier's cue.

Every path is replay-safe by construction: re-POSTing a decision on a
decided order answers 200/409 from the DB truth; re-POSTing /preparing or
/ready at the target is transition()'s idempotent no-op — and /ready
re-signals on replay, so a request that transitioned but crashed before
signalling is healed by its own retry.

Scoping: an admin's claim IS their restaurant; someone else's order (or a
nonexistent one) is the same 404 — no existence leaks.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import OrderRepo, decode_cursor, encode_cursor
from ..db import OrderStatus
from ..values import CancelReason, Verdict
from .ports import SagaGone, SagaPort
from .service import InvalidCursor, OrderNotFound
from .transitions import IllegalTransition, transition


class AlreadyDecided(Exception):
    """The decision fork is behind us and the verdicts disagree (or the
    timer decided). Repeating the SAME verdict is a 200, never this."""


class KitchenStateConflict(Exception):
    """The requested move does not fit where the order stands."""

    def __init__(self, actual: str):
        self.actual = actual
        super().__init__(f"order is {actual}")


@dataclass(frozen=True)
class DecisionResult:
    signalled: bool  # True → verdict handed to the saga (202); False → already applied (200)
    status: OrderStatus


# Once any of these is visible, the accept verdict has been applied.
_ACCEPTED_FAMILY = frozenset(
    {"ACCEPTED", "PREPARING", "READY", "PICKED_UP", "DELIVERED", "SETTLED"}
)
_CANCEL_FAMILY = frozenset({"CANCELLING", "CANCELLED", "REFUNDED"})


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


log = get_logger("order.kitchen")


class KitchenService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, saga: SagaPort):
        self._sessions = sessions
        self._saga = saga

    # ── the feed ───────────────────────────────────────────────────

    async def feed(
        self, restaurant_id: str, *, status: OrderStatus, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        after: tuple[datetime, str] | None = None
        if cursor is not None:
            try:
                after = decode_cursor(cursor)
            except (ValueError, TypeError):
                raise InvalidCursor from None
        async with self._sessions() as session:
            repo = OrderRepo(session)
            rows = await repo.list_feed(
                restaurant_id=restaurant_id, status=status, limit=limit, after=after
            )
            page, has_more = rows[:limit], len(rows) > limit
            items = await repo.get_items_for([row.order_id for row in page])
        next_cursor = (
            encode_cursor(_aware(page[-1].placed_at), page[-1].order_id) if has_more else None
        )
        return {
            "items": [self._feed_entry(row, items.get(row.order_id, [])) for row in page],
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _feed_entry(row: Row[Any], items: list[Row[Any]]) -> dict[str, Any]:
        return {
            "order_id": row.order_id,
            # Which branch this ticket belongs to — the FE joins branch
            # labels client-side for the brand-wide feed (ADR-0028).
            "restaurant_id": row.restaurant_id,
            "status": row.status,
            "placed_at": _aware(row.placed_at).isoformat(),
            "total_cents": row.pricing_snapshot["total_cents"],
            "currency": row.pricing_snapshot["currency"],
            "cancel_reason": row.cancel_reason,
            "items": [
                {"menu_item_id": item.menu_item_id, "name": item.name_snapshot, "qty": item.qty}
                for item in items
            ],
        }

    # ── accept / reject ────────────────────────────────────────────

    async def decide(self, restaurant_id: str, order_id: str, verdict: Verdict) -> DecisionResult:
        row = await self._owned(restaurant_id, order_id)
        status: OrderStatus = row.status

        if status == "CONFIRMED":
            try:
                await self._saga.signal_decision(order_id, verdict)
            except SagaGone:
                # The workflow finished between our read and the signal —
                # the timer (or a prior verdict) beat this request.
                raise AlreadyDecided("the decision window has closed") from None
            return DecisionResult(signalled=True, status=status)

        if status in _ACCEPTED_FAMILY:
            if verdict == "accept":
                return DecisionResult(signalled=False, status=status)  # same verdict: replay
            raise AlreadyDecided("order was already accepted")

        if status in _CANCEL_FAMILY:
            if row.cancel_reason == CancelReason.RESTAURANT_REJECTED:
                if verdict == "reject":
                    return DecisionResult(signalled=False, status=status)
                raise AlreadyDecided("order was already rejected")
            if row.cancel_reason == CancelReason.RESTAURANT_TIMEOUT:
                raise AlreadyDecided("the decision window timed out")
            if row.cancel_reason == CancelReason.CUSTOMER_CANCELLED:
                # S7: the customer mooted the fork — possibly AFTER this
                # kitchen's accept was submitted. Its retry must hear "the
                # window closed", not "your action doesn't fit".
                raise AlreadyDecided("the customer cancelled this order")
            # Cancelled before it ever reached the kitchen (stock, payment):
            # there was never a decision to make — a state problem, not a
            # decided-differently problem.
            raise KitchenStateConflict(status)

        # PLACED / VALIDATED / PAYMENT_CLEARED — the saga hasn't put the
        # order in front of the kitchen yet.
        raise KitchenStateConflict(status)

    # ── preparing / ready ──────────────────────────────────────────

    async def set_preparing(self, restaurant_id: str, order_id: str) -> OrderStatus:
        await self._owned(restaurant_id, order_id)
        await self._move(order_id, expected="ACCEPTED", target="PREPARING")
        return "PREPARING"

    async def set_ready(self, restaurant_id: str, order_id: str) -> OrderStatus:
        await self._owned(restaurant_id, order_id)
        await self._move(order_id, expected="PREPARING", target="READY")
        # Signal AFTER the commit, on fresh applies AND replays: a request
        # that moved the row but died before signalling heals on retry.
        try:
            await self._saga.signal_food_ready(order_id)
        except SagaGone:
            # The move committed; denying it now would be a lie. A vanished
            # delivery workflow (operator terminated it; S7 cancel) is an
            # ops problem, not the kitchen's — answer the truth, page later.
            log.warning("food_ready had no listener", order_id=order_id)
        return "READY"

    # ── helpers ────────────────────────────────────────────────────

    async def _owned(self, restaurant_id: str, order_id: str) -> Row[Any]:
        async with self._sessions() as session:
            row = await OrderRepo(session).get_order_any(order_id)
        # The claim may be the BRAND (owner) or the branch itself (old
        # tokens through the repoint window; per-branch managers later) —
        # either arm owns the order (ADR-0028).
        if row is None or restaurant_id not in (row.restaurant_id, row.brand_id):
            raise OrderNotFound  # not-found and not-yours are the same 404
        return row

    async def _move(self, order_id: str, *, expected: OrderStatus, target: OrderStatus) -> None:
        try:
            await transition(self._sessions, order_id, expected=expected, target=target)
        except IllegalTransition as exc:
            raise KitchenStateConflict(exc.actual) from None
