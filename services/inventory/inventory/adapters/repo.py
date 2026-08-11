"""All inventory SQL. The domain owns transactions; this owns statements.

Every guard lives in the WHERE clause (`available >= q`, `active < capacity`,
`status = 'active'`): 0 rows updated IS the business answer, never a race.
"""

from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from smartfood_otel import current_traceparent
from smartfood_outbox import event_id
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import outbox, reservations, restaurant_load, stock


class InventoryRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    # ── stock ──────────────────────────────────────────────────────

    async def list_stock(self, restaurant_id: str) -> list[Row[Any]]:
        result = await self._s.execute(
            sa.select(stock).where(stock.c.restaurant_id == restaurant_id).order_by(stock.c.item_id)
        )
        return list(result.all())

    async def get_stock_rows(self, restaurant_id: str, item_ids: list[str]) -> list[Row[Any]]:
        result = await self._s.execute(
            sa.select(stock).where(
                (stock.c.restaurant_id == restaurant_id) & (stock.c.item_id.in_(item_ids))
            )
        )
        return list(result.all())

    async def update_stock(
        self, restaurant_id: str, item_id: str, available: int, now: datetime
    ) -> Row[Any] | None:
        """Scoped absolute set; None = no row for this (restaurant, item)."""
        result = await self._s.execute(
            stock.update()
            .where((stock.c.item_id == item_id) & (stock.c.restaurant_id == restaurant_id))
            .values(available=available, version=stock.c.version + 1, updated_at=now)
            .returning(stock.c.available, stock.c.version)
        )
        return result.one_or_none()

    async def insert_stock(
        self, restaurant_id: str, item_id: str, available: int, now: datetime
    ) -> None:
        await self._s.execute(
            stock.insert().values(
                item_id=item_id,
                restaurant_id=restaurant_id,
                available=available,
                version=0,
                updated_at=now,
            )
        )

    async def decrement_stock(self, restaurant_id: str, item_id: str, qty: int) -> bool:
        """The oversell guard (FR-15). False = not enough (or no row = 0)."""
        result = await self._s.execute(
            stock.update()
            .where(
                (stock.c.item_id == item_id)
                & (stock.c.restaurant_id == restaurant_id)
                & (stock.c.available >= qty)
            )
            .values(available=stock.c.available - qty, version=stock.c.version + 1)
        )
        return cast(CursorResult[Any], result).rowcount == 1

    async def increment_stock(self, restaurant_id: str, item_id: str, qty: int) -> bool:
        result = await self._s.execute(
            stock.update()
            .where((stock.c.item_id == item_id) & (stock.c.restaurant_id == restaurant_id))
            .values(available=stock.c.available + qty, version=stock.c.version + 1)
        )
        return cast(CursorResult[Any], result).rowcount == 1

    # ── restaurant_load (capacity) ─────────────────────────────────

    async def get_load(self, restaurant_id: str) -> Row[Any] | None:
        result = await self._s.execute(
            sa.select(restaurant_load).where(restaurant_load.c.restaurant_id == restaurant_id)
        )
        return result.one_or_none()

    async def insert_load(self, restaurant_id: str, capacity: int) -> None:
        await self._s.execute(
            restaurant_load.insert().values(
                restaurant_id=restaurant_id, active=0, capacity=capacity, version=0
            )
        )

    async def set_capacity(self, restaurant_id: str, capacity: int) -> bool:
        result = await self._s.execute(
            restaurant_load.update()
            .where(restaurant_load.c.restaurant_id == restaurant_id)
            .values(capacity=capacity, version=restaurant_load.c.version + 1)
        )
        return cast(CursorResult[Any], result).rowcount == 1

    async def occupy_slot(self, restaurant_id: str) -> bool:
        """Capacity gate: kitchen slots as stock. False = at capacity."""
        result = await self._s.execute(
            restaurant_load.update()
            .where(
                (restaurant_load.c.restaurant_id == restaurant_id)
                & (restaurant_load.c.active < restaurant_load.c.capacity)
            )
            .values(active=restaurant_load.c.active + 1)
        )
        return cast(CursorResult[Any], result).rowcount == 1

    async def free_slot(self, restaurant_id: str) -> None:
        # `active > 0` guard: freeing twice (or after a capacity reset) must
        # never drive the counter negative.
        await self._s.execute(
            restaurant_load.update()
            .where(
                (restaurant_load.c.restaurant_id == restaurant_id) & (restaurant_load.c.active > 0)
            )
            .values(active=restaurant_load.c.active - 1)
        )

    # ── reservations ───────────────────────────────────────────────

    async def get_reservation(self, order_id: str) -> Row[Any] | None:
        result = await self._s.execute(
            sa.select(reservations).where(reservations.c.order_id == order_id)
        )
        return result.one_or_none()

    async def insert_reservation(
        self,
        order_id: str,
        restaurant_id: str,
        lines: list[dict[str, Any]],
        now: datetime,
        expires_at: datetime,
    ) -> None:
        await self._s.execute(
            reservations.insert().values(
                order_id=order_id,
                restaurant_id=restaurant_id,
                lines=lines,
                status="active",
                version=0,
                created_at=now,
                expires_at=expires_at,
            )
        )

    async def finish_reservation(self, order_id: str, target_status: str) -> Row[Any] | None:
        """Guarded active→target; None = it was not active (idempotent path)."""
        result = await self._s.execute(
            reservations.update()
            .where((reservations.c.order_id == order_id) & (reservations.c.status == "active"))
            .values(status=target_status, version=reservations.c.version + 1)
            .returning(reservations.c.restaurant_id, reservations.c.lines, reservations.c.version)
        )
        return result.one_or_none()

    async def overdue_reservations(self, now: datetime, limit: int = 100) -> list[str]:
        result = await self._s.execute(
            sa.select(reservations.c.order_id)
            .where((reservations.c.status == "active") & (reservations.c.expires_at < now))
            .order_by(reservations.c.expires_at)
            .limit(limit)
        )
        return [row.order_id for row in result.all()]

    # ── outbox ─────────────────────────────────────────────────────

    async def stage_event(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        version: int,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await self._s.execute(
            outbox.insert().values(
                id=event_id(aggregate_type, aggregate_id, version, event_type),
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                aggregate_version=version,
                event_type=event_type,
                payload=payload,
                occurred_at=now,
                traceparent=current_traceparent(),
            )
        )
