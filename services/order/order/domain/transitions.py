"""THE status writer (ARCHITECTURE §6.2). Every order-status change in this
service goes through transition() — the workflow's saga moves and (from S6)
the restaurant's kitchen moves alike. A source-scan test enforces that no
other module ever updates orders.status.

The guarded UPDATE is the referee: `WHERE status = :expected` means a
replayed or raced transition matches 0 rows. Zero rows is AMBIGUOUS, so we
re-read: already at the target → an idempotent replay (fine, applied=False);
anything else → IllegalTransition, which is non-retryable by definition —
retrying an illegal move can never make it legal.

Event staging joins the SAME transaction (invariant 1): the announcement
commits with the state change or not at all, and the event id is derived
from the post-bump aggregate_version (deterministic identity).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy as sa
from smartfood_kafka import EventType
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import tracking
from ..adapters.repo import OrderRepo
from ..db import OrderStatus, order_items, orders


class IllegalTransition(Exception):
    def __init__(self, order_id: str, expected: OrderStatus, target: OrderStatus, actual: str):
        self.actual = actual
        super().__init__(f"{order_id}: {expected}→{target} but order is {actual}")


@dataclass(frozen=True)
class TransitionResult:
    applied: bool  # False = idempotent replay (already at target)
    version: int


def _now() -> datetime:
    return datetime.now(UTC)


async def transition(
    sessions: async_sessionmaker[AsyncSession],
    order_id: str,
    *,
    expected: OrderStatus,
    target: OrderStatus,
    event: EventType | None = None,
    cancel_reason: str | None = None,
) -> TransitionResult:
    """One guarded move, one transaction, optionally one event."""
    now = _now()
    async with sessions() as session:
        values: dict[str, Any] = {
            "status": target,
            "aggregate_version": orders.c.aggregate_version + 1,
            "updated_at": now,
        }
        if cancel_reason is not None:
            values["cancel_reason"] = cancel_reason
        result = await session.execute(
            orders.update()
            .where((orders.c.order_id == order_id) & (orders.c.status == expected))
            .values(**values)
            .returning(orders.c.aggregate_version)
        )
        row = result.one_or_none()
        if row is None:
            # 0 rows is ambiguous — re-read to disambiguate.
            current = (
                await session.execute(
                    sa.select(orders.c.status, orders.c.aggregate_version).where(
                        orders.c.order_id == order_id
                    )
                )
            ).one_or_none()
            if current is not None and current.status == target:
                return TransitionResult(applied=False, version=current.aggregate_version)
            actual = current.status if current is not None else "absent"
            raise IllegalTransition(order_id, expected, target, actual)

        version = cast(int, row.aggregate_version)
        if event is not None:
            payload = await _full_state(session, order_id, target, version, now, cancel_reason)
            await OrderRepo(session).stage_event(
                order_id=order_id, version=version, event_type=event, payload=payload, now=now
            )
        await session.commit()
    # Post-commit on purpose: the stream hint must never describe a write
    # that rolled back, and its failure must never undo one that landed.
    await tracking.publish_status(order_id, target)
    return TransitionResult(applied=True, version=version)


async def begin_cancel_from(
    sessions: async_sessionmaker[AsyncSession],
    order_id: str,
    *,
    allowed: tuple[OrderStatus, ...],
    reason: str,
) -> bool:
    """The SET-guarded move for races the caller cannot pin to one state:
    a customer cancel arriving mid-kitchen may find ACCEPTED, PREPARING or
    READY — any of them may become CANCELLING, but PICKED_UP may not (the
    courier holds the food; FR-21). True = the order is now CANCELLING
    (fresh apply, or an at-least-once replay finding it there — only this
    order's own workflow ever writes CANCELLING, so a replay is always
    ours); False = the courier won the race, cancellation refused."""
    async with sessions() as session:
        result = await session.execute(
            orders.update()
            .where((orders.c.order_id == order_id) & (orders.c.status.in_(allowed)))
            .values(
                status="CANCELLING",
                cancel_reason=reason,
                aggregate_version=orders.c.aggregate_version + 1,
                updated_at=_now(),
            )
            .returning(orders.c.aggregate_version)
        )
        applied = result.one_or_none() is not None
        await session.commit()
        if applied:
            await tracking.publish_status(order_id, "CANCELLING")
            return True
        current = (
            await session.execute(sa.select(orders.c.status).where(orders.c.order_id == order_id))
        ).scalar_one_or_none()
    return current == "CANCELLING"


async def _full_state(
    session: AsyncSession,
    order_id: str,
    status: OrderStatus,
    version: int,
    now: datetime,
    cancel_reason: str | None,
) -> dict[str, Any]:
    """Order events carry full state (the OrderPlaced payload shape) so any
    consumer can act on one event alone."""
    order = (await session.execute(sa.select(orders).where(orders.c.order_id == order_id))).one()
    items = (
        await session.execute(
            sa.select(order_items)
            .where(order_items.c.order_id == order_id)
            .order_by(order_items.c.line_no)
        )
    ).all()
    return {
        "order_id": order_id,
        "user_id": order.user_id,
        "restaurant_id": order.restaurant_id,
        "restaurant_name": order.restaurant_name_snapshot,
        "status": status,
        "aggregate_version": version,
        "menu_version": order.menu_version,
        "items": [
            {
                "menu_item_id": item.menu_item_id,
                "name": item.name_snapshot,
                "qty": item.qty,
                "unit_price_cents": item.unit_price_cents,
                "line_total_cents": item.line_total_cents,
            }
            for item in items
        ],
        "totals": order.pricing_snapshot,
        "delivery_address": order.delivery_address_snapshot,
        "cancel_reason": cancel_reason,
        "occurred_at": now.isoformat(),
    }
