"""Domain artifacts HTTP assertions can't see: outbox staging for stock
mutations, upsert race branches, deterministic event ids."""

from datetime import UTC, datetime

import sqlalchemy as sa
from inventory.adapters.repo import InventoryRepo
from inventory.db import metadata, outbox
from inventory.domain.models import ReservationLine
from inventory.domain.service import InventoryService
from smartfood_outbox import event_id
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


async def test_set_stock_stages_full_state_event():
    svc, sessions = await _service()
    await svc.set_stock("rst_1", "itm_a", 25)
    await svc.set_stock("rst_1", "itm_a", 30)
    async with sessions() as s:
        events = (await s.execute(sa.select(outbox).order_by(outbox.c.aggregate_version))).all()
    assert [e.event_type for e in events] == ["StockAdjusted", "StockAdjusted"]
    assert events[0].payload == {
        "item_id": "itm_a",
        "restaurant_id": "rst_1",
        "available": 25,
        "version": 0,
    }
    assert events[1].payload["available"] == 30
    # Aggregate = (branch, item): a shared base item has an independent
    # ledger per branch (ADR-0028) — item_id alone would collide ids.
    assert events[1].id == event_id("stock", "rst_1:itm_a", 1, "StockAdjusted")


async def _win_race_then_collide(sessions, restaurant_id: str, capacity: int):
    """Build an insert_load stand-in that loses a REAL race: a concurrent
    session commits the row first, then the genuine conflict-safe insert
    runs — and answers False through the actual ON CONFLICT path."""
    real_insert = InventoryRepo.insert_load  # captured BEFORE the patch

    async def racing_insert(self, rid, cap):
        async with sessions() as other:  # the winner — a different tx entirely
            await real_insert(InventoryRepo(other), restaurant_id, capacity)
            await other.commit()
        return await real_insert(self, rid, cap)  # the loser: False, no abort

    return racing_insert


async def test_set_capacity_concurrent_creation_race(monkeypatch):
    """Loser of the insert race rolls back and takes the update path."""
    svc, sessions = await _service()
    monkeypatch.setattr(
        InventoryRepo, "insert_load", await _win_race_then_collide(sessions, "rst_1", 5)
    )
    capacity, active = await svc.set_capacity("rst_1", 7)
    assert (capacity, active) == (7, 0)  # loser's UPDATE won over the winner's 5


async def test_ensure_load_row_concurrent_creation_race(monkeypatch):
    """reserve()'s load-row bootstrap: IntegrityError → rollback → proceed
    against the winner's committed row."""
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 5)
    monkeypatch.setattr(
        InventoryRepo, "insert_load", await _win_race_then_collide(sessions, "rst_1", 10)
    )
    reservation, created = await svc.reserve(
        order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 1))
    )
    assert created  # the race didn't break the reserve
    _, active = await svc.set_capacity("rst_1", 10)
    assert active == 1  # the slot count survived the mid-flight rollback
