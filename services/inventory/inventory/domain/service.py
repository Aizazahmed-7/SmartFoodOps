"""Inventory domain — stock, capacity, and the reservation lifecycle.

STRICT stock (user decision): every item is tracked and starts at 0; a
missing stock row reserves exactly like a zero row. The reservation ledger
is keyed by order_id, which makes every saga retry idempotent by
construction — the second reserve for an order replays the first.

Reserve is all-or-nothing in ONE transaction: capacity slot + every line's
conditional decrement, or none of it. The pre-check produces friendly
per-line details; the conditional UPDATE remains the true oversell guard
(a concurrent order can still win between check and decrement).
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from smartfood_kafka import EventType
from smartfood_otel import get_logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import InventoryRepo
from ..db import ReservationStatus
from .models import Reservation, ReservationLine, StockRow

log = get_logger("inventory.service")


class InsufficientStock(Exception):
    def __init__(self, item_ids: list[str]):
        self.item_ids = item_ids
        super().__init__(f"insufficient stock: {item_ids}")


class AtCapacity(Exception):
    pass


class StockScopeMismatch(Exception):
    """The item exists but belongs to another restaurant — surfaces as 404."""


def _now() -> datetime:
    return datetime.now(UTC)


def _reservation_of(row: Any) -> Reservation:
    raw: Any = row.lines  # JSON column: list on PG, str on some sqlite paths
    lines = cast(list[dict[str, Any]], raw if isinstance(raw, list) else json.loads(raw))
    return Reservation(
        order_id=row.order_id,
        restaurant_id=row.restaurant_id,
        lines=tuple(ReservationLine(item_id=li["item_id"], qty=li["qty"]) for li in lines),
        status=row.status,
        expires_at=row.expires_at,
    )


class InventoryService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        default_capacity: int = 10,
        reservation_ttl_seconds: int = 1800,
    ):
        self._sessions = sessions
        self._default_capacity = default_capacity
        self._ttl = reservation_ttl_seconds

    # ── admin: stock & capacity ────────────────────────────────────

    async def list_stock(self, restaurant_id: str) -> list[StockRow]:
        async with self._sessions() as session:
            rows = await InventoryRepo(session).list_stock(restaurant_id)
        return [
            StockRow(
                item_id=r.item_id,
                restaurant_id=r.restaurant_id,
                available=r.available,
                version=r.version,
            )
            for r in rows
        ]

    async def set_stock(self, restaurant_id: str, item_id: str, available: int) -> StockRow:
        """Absolute set (PUT semantics — replayable). Upsert: the admin may
        legitimately beat the catalog.changes consumer to a brand-new item."""
        now = _now()
        async with self._sessions() as session:
            repo = InventoryRepo(session)
            updated = await repo.update_stock(restaurant_id, item_id, available, now)
            if updated is None:
                try:
                    await repo.insert_stock(restaurant_id, item_id, available, now)
                    version = 0
                except IntegrityError:
                    # Row exists under ANOTHER restaurant (scoped update saw
                    # nothing, PK insert collided) — not yours, 404.
                    raise StockScopeMismatch from None
            else:
                version = updated.version
            await repo.stage_event(
                aggregate_type="stock",
                aggregate_id=item_id,
                version=version,
                event_type=EventType.STOCK_ADJUSTED,
                payload={
                    "item_id": item_id,
                    "restaurant_id": restaurant_id,
                    "available": available,
                    "version": version,
                },
                now=now,
            )
            await session.commit()
        return StockRow(
            item_id=item_id, restaurant_id=restaurant_id, available=available, version=version
        )

    async def set_capacity(self, restaurant_id: str, capacity: int) -> tuple[int, int]:
        """Returns (capacity, active). Lowering below current active is legal:
        new orders stop, running ones drain."""
        async with self._sessions() as session:
            repo = InventoryRepo(session)
            if not await repo.set_capacity(restaurant_id, capacity):
                try:
                    await repo.insert_load(restaurant_id, capacity)
                except IntegrityError:  # concurrent creation — take the update path.
                    # PG aborts the tx on constraint failure: rollback FIRST
                    # (sqlite forgives this; Postgres does not).
                    await session.rollback()
                    await repo.set_capacity(restaurant_id, capacity)
            load = await repo.get_load(restaurant_id)
            assert load is not None  # just written
            await session.commit()
            return load.capacity, load.active

    # ── saga: reserve / release / commit ───────────────────────────

    async def reserve(
        self,
        *,
        order_id: str,
        restaurant_id: str,
        lines: list[ReservationLine],
        ttl_seconds: int | None = None,
    ) -> tuple[Reservation, bool]:
        """Returns (reservation, created). Replay (PK hit) returns the
        existing row untouched — the saga activity's natural idempotency."""
        now = _now()
        async with self._sessions() as session:
            repo = InventoryRepo(session)
            existing = await repo.get_reservation(order_id)
            if existing is not None:
                return _reservation_of(existing), False

            await self._ensure_load_row(session, repo, restaurant_id)
            if not await repo.occupy_slot(restaurant_id):
                raise AtCapacity

            # Friendly pre-check: report EVERY short line, not just the first.
            by_id = {
                r.item_id: r.available
                for r in await repo.get_stock_rows(restaurant_id, [li.item_id for li in lines])
            }
            short = sorted(li.item_id for li in lines if by_id.get(li.item_id, 0) < li.qty)
            if short:
                raise InsufficientStock(short)  # tx discards the slot occupy
            for line in lines:
                if not await repo.decrement_stock(restaurant_id, line.item_id, line.qty):
                    # Lost a race between pre-check and decrement: the WHERE
                    # clause is the real guard. Rollback releases everything.
                    raise InsufficientStock([line.item_id])

            expires_at = now + timedelta(seconds=ttl_seconds or self._ttl)
            line_dicts = [{"item_id": li.item_id, "qty": li.qty} for li in lines]
            await repo.insert_reservation(order_id, restaurant_id, line_dicts, now, expires_at)
            await repo.stage_event(
                aggregate_type="reservation",
                aggregate_id=order_id,
                version=0,
                event_type=EventType.STOCK_RESERVED,
                payload={
                    "order_id": order_id,
                    "restaurant_id": restaurant_id,
                    "lines": line_dicts,
                    "status": "active",
                },
                now=now,
            )
            await session.commit()
        return (
            Reservation(
                order_id=order_id,
                restaurant_id=restaurant_id,
                lines=tuple(lines),
                status="active",
                expires_at=expires_at,
            ),
            True,
        )

    async def release(self, order_id: str, *, reason: str) -> bool:
        """Compensation: restore stock, free the slot. reason='expired' is the
        reaper's path (distinct terminal status). Not-active → False (no-op)."""
        target: ReservationStatus = "expired" if reason == "expired" else "released"
        now = _now()
        async with self._sessions() as session:
            repo = InventoryRepo(session)
            finished = await repo.finish_reservation(order_id, target)
            if finished is None:
                return False
            for line in finished.lines:
                # A vanished stock row can't be restored — skip, never fail
                # the compensation (compensations must always succeed).
                await repo.increment_stock(finished.restaurant_id, line["item_id"], line["qty"])
            await repo.free_slot(finished.restaurant_id)
            await repo.stage_event(
                aggregate_type="reservation",
                aggregate_id=order_id,
                version=finished.version,
                event_type=EventType.RESERVATION_RELEASED,
                payload={
                    "order_id": order_id,
                    "restaurant_id": finished.restaurant_id,
                    "lines": finished.lines,
                    "status": target,
                    "reason": reason,
                },
                now=now,
            )
            await session.commit()
        return True

    async def commit(self, order_id: str) -> bool:
        """Settlement: the sale is final — stock stays decremented, only the
        kitchen slot frees. Not-active → False (idempotent replay / too late)."""
        now = _now()
        async with self._sessions() as session:
            repo = InventoryRepo(session)
            finished = await repo.finish_reservation(order_id, "consumed")
            if finished is None:
                return False
            await repo.free_slot(finished.restaurant_id)
            await repo.stage_event(
                aggregate_type="reservation",
                aggregate_id=order_id,
                version=finished.version,
                event_type=EventType.RESERVATION_CONSUMED,
                payload={
                    "order_id": order_id,
                    "restaurant_id": finished.restaurant_id,
                    "lines": finished.lines,
                    "status": "consumed",
                },
                now=now,
            )
            await session.commit()
        return True

    async def expire_overdue(self) -> int:
        """One reaper pass. Separate short transactions per reservation so a
        single failure can't wedge the whole batch."""
        async with self._sessions() as session:
            overdue = await InventoryRepo(session).overdue_reservations(_now())
        expired = 0
        for order_id in overdue:
            if await self.release(order_id, reason="expired"):
                log.info("reservation expired by reaper", order_id=order_id)
                expired += 1
        return expired

    # ── helpers ────────────────────────────────────────────────────

    async def _ensure_load_row(
        self, session: AsyncSession, repo: InventoryRepo, restaurant_id: str
    ) -> None:
        """Runs BEFORE any write in reserve(): the rollback on the race path
        only discards reads, never a partial reservation."""
        if await repo.get_load(restaurant_id) is None:
            try:
                await repo.insert_load(restaurant_id, self._default_capacity)
            except IntegrityError:  # concurrent creation — row now exists.
                # PG aborts the tx on constraint failure: rollback before the
                # caller's next statement (sqlite forgives this; PG does not).
                await session.rollback()
