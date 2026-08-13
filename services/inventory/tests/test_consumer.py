"""Stock provisioning from catalog.changes: NATURAL_KEY idempotency —
replaying any event must be a no-op; admin counts must survive menu edits.
The loop that feeds this handler is smartfood_kafka.EventConsumer, tested
in its own lib."""

import json

import sqlalchemy as sa
from inventory.adapters.repo import InventoryRepo
from inventory.consumers import StockProvisioningHandler
from inventory.db import metadata, restaurant_load, stock
from inventory.domain.service import InventoryService
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


def _catalog_event(restaurant_id="rst_1", item_ids=("itm_a", "itm_b"), event_type="ItemAdded"):
    payload = {
        "id": restaurant_id,
        "menu": {
            "categories": [{"id": "cat_1", "items": [{"id": item_id} for item_id in item_ids]}]
        },
    }
    return {
        "event_id": "e1",
        "event_type": event_type,
        "aggregate_id": restaurant_id,
        "payload": json.dumps(payload),
    }


async def test_new_items_provisioned_at_zero_with_default_load():
    _, sessions = await _service()
    handler = StockProvisioningHandler(sessions, default_capacity=10)
    await handler.handle(_catalog_event())
    async with sessions() as s:
        rows = {r.item_id: r for r in (await s.execute(sa.select(stock))).all()}
        load = (await s.execute(sa.select(restaurant_load))).one()
    assert set(rows) == {"itm_a", "itm_b"}
    assert all(r.available == 0 for r in rows.values())  # STRICT stock: born at 0
    assert (load.capacity, load.active) == (10, 0)


async def test_existing_counts_survive_menu_edits():
    svc, sessions = await _service()
    await svc.set_stock("rst_1", "itm_a", 42)  # admin already stocked it
    handler = StockProvisioningHandler(sessions)
    await handler.handle(_catalog_event(item_ids=("itm_a", "itm_new")))
    async with sessions() as s:
        rows = {r.item_id: r.available for r in (await s.execute(sa.select(stock))).all()}
    assert rows == {"itm_a": 42, "itm_new": 0}  # never reset, only fill gaps


async def test_replay_is_a_noop():
    _, sessions = await _service()
    handler = StockProvisioningHandler(sessions)
    await handler.handle(_catalog_event())
    await handler.handle(_catalog_event())  # at-least-once delivery
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(stock))).scalar_one()
    assert count == 2


async def test_malformed_payload_logged_not_poisonous():
    _, sessions = await _service()
    handler = StockProvisioningHandler(sessions)
    await handler.handle({"event_id": "e9", "payload": "not json"})
    await handler.handle({"event_id": "e10", "payload": json.dumps({"no": "menu"})})
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(stock))).scalar_one()
    assert count == 0  # skipped, no crash — the partition moves on


async def test_racing_admin_put_wins(monkeypatch):
    """Consumer loses the insert race to a concurrent admin PUT: rollback,
    keep going, admin's row intact."""
    svc, sessions = await _service()
    real_insert = InventoryRepo.insert_stock

    async def racing_insert(self, restaurant_id, item_id, available, now):
        if item_id == "itm_a":
            async with sessions() as other:  # the admin's PUT commits first
                await real_insert(InventoryRepo(other), restaurant_id, item_id, 7, now)
                await other.commit()
        return await real_insert(self, restaurant_id, item_id, available, now)

    monkeypatch.setattr(InventoryRepo, "insert_stock", racing_insert)
    handler = StockProvisioningHandler(sessions)
    await handler.handle(_catalog_event(item_ids=("itm_a", "itm_b")))
    async with sessions() as s:
        rows = {r.item_id: r.available for r in (await s.execute(sa.select(stock))).all()}
    assert rows["itm_a"] == 7  # admin's value survived
    assert rows["itm_b"] == 0  # and the rest still provisioned


async def test_mid_batch_race_never_discards_earlier_provisioning(monkeypatch):
    """THE regression the old try/except/rollback protocol had: a conflict
    on a LATER item rolled back the same batch's EARLIER uncommitted
    inserts, silently dropping provisioning until the next full-menu event.
    With conflict-safe inserts the transaction survives the lost race."""
    svc, sessions = await _service()
    real_insert = InventoryRepo.insert_stock

    async def racing_insert(self, restaurant_id, item_id, available, now):
        if item_id == "itm_b":  # the SECOND item loses a race…
            async with sessions() as other:
                await real_insert(InventoryRepo(other), restaurant_id, item_id, 9, now)
                await other.commit()
        return await real_insert(self, restaurant_id, item_id, available, now)

    monkeypatch.setattr(InventoryRepo, "insert_stock", racing_insert)
    handler = StockProvisioningHandler(sessions)
    await handler.handle(_catalog_event(item_ids=("itm_a", "itm_b")))
    async with sessions() as s:
        rows = {r.item_id: r.available for r in (await s.execute(sa.select(stock))).all()}
    assert rows["itm_a"] == 0  # …and itm_a's earlier provisioning SURVIVES
    assert rows["itm_b"] == 9  # the racing winner's value stands


async def test_load_row_race_winner_stands(monkeypatch):
    _, sessions = await _service()
    real_insert = InventoryRepo.insert_load

    async def racing_insert(self, restaurant_id, capacity):
        async with sessions() as other:
            await real_insert(InventoryRepo(other), restaurant_id, 3)
            await other.commit()
        return await real_insert(self, restaurant_id, capacity)

    monkeypatch.setattr(InventoryRepo, "insert_load", racing_insert)
    handler = StockProvisioningHandler(sessions)
    await handler.handle(_catalog_event())
    async with sessions() as s:
        load = (await s.execute(sa.select(restaurant_load))).one()
    assert load.capacity == 3  # winner's row stands
