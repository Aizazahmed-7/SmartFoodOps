"""The reservation lifecycle — every branch of reserve/release/commit, at
domain level (DB artifacts) plus the HTTP error mapping."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from inventory.adapters.repo import InventoryRepo
from inventory.db import metadata, outbox, reservations, restaurant_load, stock
from inventory.domain.models import ReservationLine
from inventory.domain.service import AtCapacity, InsufficientStock, InventoryService
from smartfood_auth import AuthContext, headers_for
from smartfood_outbox import event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


def admin(restaurant_id: str) -> dict[str, str]:
    return headers_for(
        AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=restaurant_id)
    )


SYSTEM = headers_for(AuthContext(sub="svc:order-worker", role="system"))


async def _service(**kwargs) -> tuple[InventoryService, async_sessionmaker]:
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


async def _one(sessions, table):
    async with sessions() as s:
        return (await s.execute(sa.select(table))).one()


async def test_reserve_happy_path_decrements_and_stages():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 10)
    reservation, created = await svc.reserve(
        order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 3))
    )
    assert created and reservation.status == "active"
    async with sessions() as s:
        row = (await s.execute(sa.select(stock))).one()
        load = (await s.execute(sa.select(restaurant_load))).one()
        event = (await s.execute(sa.select(outbox))).one()
    assert row.available == 7 and row.version == 1
    assert load.active == 1  # slot occupied; load row auto-created
    assert event.event_type == "StockReserved"
    assert event.id == event_id("reservation", "ord_1", 0, "StockReserved")


async def test_reserve_exact_boundary_succeeds():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 3)
    _, created = await svc.reserve(
        order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 3))
    )
    assert created
    row = await _one(sessions, stock)
    assert row.available == 0


async def test_insufficient_stock_rolls_everything_back():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 10)
    await _stock(sessions, "itm_b", 1)
    with pytest.raises(InsufficientStock) as exc:
        await svc.reserve(
            order_id="ord_1",
            restaurant_id="rst_1",
            lines=_lines(("itm_a", 2), ("itm_b", 5)),
        )
    assert exc.value.item_ids == ["itm_b"]
    async with sessions() as s:
        rows = {r.item_id: r.available for r in (await s.execute(sa.select(stock))).all()}
        load = (await s.execute(sa.select(restaurant_load))).one_or_none()
        count = (await s.execute(sa.select(sa.func.count()).select_from(outbox))).scalar_one()
    assert rows == {"itm_a": 10, "itm_b": 1}  # nothing decremented
    # The occupied slot rolled back with everything else — the auto-created
    # load row itself was part of the discarded transaction.
    assert load is None or load.active == 0
    assert count == 0


async def test_missing_stock_row_reserves_like_zero():
    svc, _ = await _service()
    with pytest.raises(InsufficientStock) as exc:
        await svc.reserve(order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_ghost", 1)))
    assert exc.value.item_ids == ["itm_ghost"]


async def test_all_short_lines_reported_sorted():
    svc, sessions = await _service()
    await _stock(sessions, "itm_z", 0)
    await _stock(sessions, "itm_a", 0)
    with pytest.raises(InsufficientStock) as exc:
        await svc.reserve(
            order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_z", 1), ("itm_a", 1))
        )
    assert exc.value.item_ids == ["itm_a", "itm_z"]


async def test_precheck_race_still_guarded_by_conditional_update(monkeypatch):
    """The WHERE clause is the real oversell guard: force the friendly
    pre-check to lie, and the decrement must still refuse."""
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 1)

    async def lying_get_stock_rows(self, restaurant_id, item_ids):
        class Row:
            item_id = "itm_a"
            available = 999  # the lie

        return [Row()]

    monkeypatch.setattr(InventoryRepo, "get_stock_rows", lying_get_stock_rows)
    with pytest.raises(InsufficientStock) as exc:
        await svc.reserve(order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 5)))
    assert exc.value.item_ids == ["itm_a"]
    row = await _one(sessions, stock)
    assert row.available == 1  # rollback proof


async def test_at_capacity_leaves_stock_untouched():
    svc, sessions = await _service(default_capacity=1)
    await _stock(sessions, "itm_a", 10)
    await svc.reserve(order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 1)))
    with pytest.raises(AtCapacity):
        await svc.reserve(order_id="ord_2", restaurant_id="rst_1", lines=_lines(("itm_a", 1)))
    row = await _one(sessions, stock)
    assert row.available == 9  # only the first order's decrement


async def test_reserve_replay_returns_existing_without_double_decrement():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 10)
    _, first = await svc.reserve(
        order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 3))
    )
    replay, second = await svc.reserve(
        order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 3))
    )
    assert first and not second
    assert replay.status == "active"
    row = await _one(sessions, stock)
    assert row.available == 7  # exactly one decrement


async def test_release_restores_stock_and_slot_then_noops():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 10)
    await svc.reserve(order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 4)))
    assert await svc.release("ord_1", reason="cancelled") is True
    assert await svc.release("ord_1", reason="cancelled") is False  # idempotent no-op
    async with sessions() as s:
        row = (await s.execute(sa.select(stock))).one()
        load = (await s.execute(sa.select(restaurant_load))).one()
        reservation = (await s.execute(sa.select(reservations))).one()
        types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    assert row.available == 10 and load.active == 0
    assert reservation.status == "released"
    assert types == ["StockReserved", "ReservationReleased"]


async def test_release_survives_vanished_stock_row():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 5)
    await svc.reserve(order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 2)))
    async with sessions() as s:  # simulate an impossible-but-defended deletion
        await s.execute(stock.delete())
        await s.commit()
    assert await svc.release("ord_1", reason="cancelled") is True  # never fails


async def test_commit_keeps_stock_frees_slot_then_noops():
    svc, sessions = await _service()
    await _stock(sessions, "itm_a", 10)
    await svc.reserve(order_id="ord_1", restaurant_id="rst_1", lines=_lines(("itm_a", 4)))
    assert await svc.commit("ord_1") is True
    assert await svc.commit("ord_1") is False  # replay no-op
    assert await svc.release("ord_1", reason="cancelled") is False  # too late — consumed won
    async with sessions() as s:
        row = (await s.execute(sa.select(stock))).one()
        load = (await s.execute(sa.select(restaurant_load))).one()
        reservation = (await s.execute(sa.select(reservations))).one()
    assert row.available == 6  # the sale is final
    assert load.active == 0
    assert reservation.status == "consumed"


# ── HTTP mapping (system-only surface) ─────────────────────────────


def _reserve_body(order_id="ord_1", qty=1):
    return {
        "order_id": order_id,
        "restaurant_id": "rst_1",
        "lines": [{"item_id": "itm_a", "qty": qty}],
    }


def test_http_reserve_created_then_replayed(client):
    client.put(
        "/v1/inventory/restaurants/rst_1/stock/itm_a",
        json={"available": 5},
        headers=admin("rst_1"),
    )
    first = client.post("/v1/internal/reservations", json=_reserve_body(), headers=SYSTEM)
    assert first.status_code == 201
    assert first.json()["status"] == "active"
    replay = client.post("/v1/internal/reservations", json=_reserve_body(), headers=SYSTEM)
    assert replay.status_code == 200  # replay, not a second reservation


def test_http_insufficient_maps_to_item_unavailable(client):
    r = client.post("/v1/internal/reservations", json=_reserve_body(qty=3), headers=SYSTEM)
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "ITEM_UNAVAILABLE"
    assert body["details"] == [{"item_id": "itm_a", "issue": "insufficient stock"}]


def test_http_capacity_maps_to_restaurant_at_capacity(client):
    client.put(
        "/v1/inventory/restaurants/rst_1/stock/itm_a",
        json={"available": 50},
        headers=admin("rst_1"),
    )
    client.put(
        "/v1/inventory/restaurants/rst_1/capacity", json={"capacity": 1}, headers=admin("rst_1")
    )
    assert (
        client.post(
            "/v1/internal/reservations", json=_reserve_body("ord_1"), headers=SYSTEM
        ).status_code
        == 201
    )
    r = client.post("/v1/internal/reservations", json=_reserve_body("ord_2"), headers=SYSTEM)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESTAURANT_AT_CAPACITY"


def test_http_release_and_commit_roundtrip(client):
    client.put(
        "/v1/inventory/restaurants/rst_1/stock/itm_a",
        json={"available": 5},
        headers=admin("rst_1"),
    )
    client.post("/v1/internal/reservations", json=_reserve_body(), headers=SYSTEM)
    released = client.post(
        "/v1/internal/reservations/ord_1/release", json={"reason": "cancelled"}, headers=SYSTEM
    )
    assert released.json() == {"order_id": "ord_1", "released": True}
    committed = client.post("/v1/internal/reservations/ord_1/commit", headers=SYSTEM)
    assert committed.json() == {"order_id": "ord_1", "committed": False}  # already released


def test_internal_routes_reject_non_system(client):
    assert (
        client.post(
            "/v1/internal/reservations", json=_reserve_body(), headers=admin("rst_1")
        ).status_code
        == 403
    )


def test_reserve_dto_bounds(client):
    bad = _reserve_body()
    bad["lines"] = []
    assert client.post("/v1/internal/reservations", json=bad, headers=SYSTEM).status_code == 422
    bad = _reserve_body()
    bad["ttl_seconds"] = 10  # below the 60s floor
    assert client.post("/v1/internal/reservations", json=bad, headers=SYSTEM).status_code == 422
