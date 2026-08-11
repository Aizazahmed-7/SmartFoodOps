"""The expiry reaper: only overdue actives die; the loop survives bad passes."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from inventory.adapters.repo import InventoryRepo
from inventory.db import metadata, reservations, stock
from inventory.domain.models import ReservationLine
from inventory.domain.service import InventoryService
from inventory.reaper import Reaper
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def _service(**kwargs):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return InventoryService(sessions, **kwargs), sessions


async def _stock(sessions, item_id: str, available: int, restaurant_id: str = "rst_1"):
    async with sessions() as s:
        await InventoryRepo(s).insert_stock(restaurant_id, item_id, available, datetime.now(UTC))
        await s.commit()


def _lines(*pairs: tuple[str, int]) -> list[ReservationLine]:
    return [ReservationLine(item_id=i, qty=q) for i, q in pairs]


async def _age_reservation(sessions, order_id: str, seconds: int):
    async with sessions() as s:
        await s.execute(
            reservations.update()
            .where(reservations.c.order_id == order_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await s.commit()


async def test_expires_only_overdue_actives():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 10)
    await svc.reserve(order_id="ord_old", restaurant_id="rst_1", lines=_lines(("itm_a", 2)))
    await svc.reserve(order_id="ord_fresh", restaurant_id="rst_1", lines=_lines(("itm_a", 3)))
    await svc.reserve(order_id="ord_done", restaurant_id="rst_1", lines=_lines(("itm_a", 1)))
    await svc.commit("ord_done")  # consumed — not the reaper's business
    await _age_reservation(sessions, "ord_old", 60)
    await _age_reservation(sessions, "ord_done", 60)

    assert await svc.expire_overdue() == 1  # only ord_old

    async with sessions() as s:
        by_id = {r.order_id: r.status for r in (await s.execute(sa.select(reservations))).all()}
        row = (await s.execute(sa.select(stock))).one()
    assert by_id == {"ord_old": "expired", "ord_fresh": "active", "ord_done": "consumed"}
    assert row.available == 10 - 3 - 1  # ord_old's 2 restored; fresh + consumed keep theirs


async def test_reaper_loop_survives_failures_and_cancels():
    class FlakyService:
        def __init__(self):
            self.calls = 0

        async def expire_overdue(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("db hiccup")
            return 0

    flaky = FlakyService()
    reaper = Reaper(flaky, interval_seconds=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(reaper.run())
    for _ in range(200):
        if flaky.calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert flaky.calls >= 2  # kept going after the failed pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancellation_mid_pass_propagates():
    """Shutdown during a pass must cancel, not be swallowed as a failure."""
    entered = asyncio.Event()

    class BlockingService:
        async def expire_overdue(self):
            entered.set()
            await asyncio.sleep(3600)

    reaper = Reaper(BlockingService(), interval_seconds=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(reaper.run())
    await entered.wait()  # cancellation lands INSIDE expire_overdue
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
