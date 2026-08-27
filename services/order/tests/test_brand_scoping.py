"""Brands slice 4 (ADR-0028): brand_id rides placement → row → events, the
kitchen scopes by brand-OR-branch, and the repoint consumer heals legacy rows."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from order.config import Settings
from order.consumers import BrandRepointHandler
from order.db import metadata, orders, outbox
from order.main import create_app
from smartfood_auth import AuthContext, headers_for
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BRAND_OWNER = headers_for(AuthContext(sub="usr_9", role="restaurant_admin", restaurant_id="brd_1"))
BRANCH_OWNER = headers_for(AuthContext(sub="usr_9", role="restaurant_admin", restaurant_id="rst_1"))
STRANGER = headers_for(AuthContext(sub="usr_8", role="restaurant_admin", restaurant_id="brd_666"))

TO_CONFIRMED = [
    ("PLACED", "VALIDATED"),
    ("VALIDATED", "PAYMENT_CLEARED"),
    ("PAYMENT_CLEARED", "CONFIRMED"),
]


@pytest.fixture()
def client(catalog, identity, saga, db_url, make_snapshot):
    catalog.snapshot = make_snapshot(brand_id="brd_1", display_name="Biryani House — Downtown")
    settings = Settings(database_url=db_url, create_all=True)
    app = create_app(settings, catalog=catalog, identity=identity, saga=saga)
    saga.bind(app.state.sessions)
    with TestClient(app) as c:
        yield c


def _read(db_url: str, query):
    async def _go():
        engine = create_async_engine(db_url)
        try:
            async with async_sessionmaker(engine)() as s:
                return (await s.execute(query)).all()
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def test_placement_stamps_brand_and_branch_labeled_name(client, place_order, db_url):
    place_order(client)
    (order,) = _read(db_url, sa.select(orders))
    assert order.brand_id == "brd_1"
    assert order.restaurant_name_snapshot == "Biryani House — Downtown"
    (event,) = _read(db_url, sa.select(outbox).where(outbox.c.event_type == "OrderPlaced"))
    payload = event.payload if isinstance(event.payload, dict) else json.loads(event.payload)
    assert payload["brand_id"] == "brd_1"
    assert payload["restaurant_name"] == "Biryani House — Downtown"


def test_feed_and_decisions_answer_to_brand_and_branch_claims(
    client, place_order, advance_order, db_url
):
    order_id = place_order(client)
    advance_order(db_url, order_id, TO_CONFIRMED)

    for claims in (BRAND_OWNER, BRANCH_OWNER):  # both arms of the OR
        feed = client.get("/v1/restaurant/orders?status=CONFIRMED", headers=claims)
        assert feed.status_code == 200
        entries = feed.json()["items"]
        assert [e["order_id"] for e in entries] == [order_id]
        assert entries[0]["restaurant_id"] == "rst_1"  # the branch the ticket belongs to

    stranger = client.get("/v1/restaurant/orders?status=CONFIRMED", headers=STRANGER)
    assert stranger.json()["items"] == []  # someone else's brand sees nothing
    denied = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=STRANGER)
    assert denied.status_code == 404  # not-yours is the one 404

    accepted = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=BRAND_OWNER)
    assert accepted.status_code == 202  # the brand claim drives the kitchen


# ── the repoint consumer ───────────────────────────────────────────


def _event(aggregate_id: str, payload: dict) -> dict:
    return {
        "event_id": "evt-1",
        "event_type": "RestaurantUpdated",
        "aggregate_type": "restaurant",
        "aggregate_id": aggregate_id,
        "payload": json.dumps(payload),
    }


async def _repoint_world():
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return BrandRepointHandler(sessions), sessions


async def _insert_order(sessions, order_id: str, restaurant_id: str, brand_id: str | None):
    now = datetime.now(UTC)
    async with sessions() as s:
        await s.execute(
            orders.insert().values(
                order_id=order_id,
                user_id="usr_1",
                restaurant_id=restaurant_id,
                brand_id=brand_id,
                restaurant_name_snapshot="Biryani House",
                status="PLACED",
                aggregate_version=0,
                card_token="tok_ok",
                menu_version=3,
                pricing_snapshot={},
                delivery_address_snapshot={},
                placed_at=now,
                updated_at=now,
            )
        )
        await s.commit()


async def test_repoint_fills_only_null_brand_rows():
    handler, sessions = await _repoint_world()
    await _insert_order(sessions, "ord_legacy", "rst_1", None)  # what this handler exists for
    await _insert_order(sessions, "ord_new", "rst_1", "brd_1")  # already stamped at placement
    await _insert_order(sessions, "ord_other", "rst_2", None)  # a different branch — untouched

    await handler.handle(_event("rst_1", {"owner_user_id": "u", "brand_id": "brd_1"}))
    await handler.handle(_event("rst_1", {"owner_user_id": "u", "brand_id": "brd_1"}))  # replay

    async with sessions() as s:
        rows = {
            r.order_id: r.brand_id
            for r in (await s.execute(sa.select(orders.c.order_id, orders.c.brand_id))).all()
        }
    assert rows == {"ord_legacy": "brd_1", "ord_new": "brd_1", "ord_other": None}


async def test_repoint_ignores_brand_aggregates_and_brandless_payloads():
    handler, sessions = await _repoint_world()
    await handler.handle(_event("brd_1", {"owner_user_id": "u", "brand_id": "brd_1"}))  # brand row
    await handler.handle(_event("rst_1", {"owner_user_id": "u"}))  # pre-brands payload
    await handler.handle(_event("rst_1", {"owner_user_id": "u", "brand_id": None}))  # transitional
    non_restaurant = _event("stk_1", {"brand_id": "brd_1"})
    non_restaurant["aggregate_type"] = "stock"
    await handler.handle(non_restaurant)
    async with sessions() as s:  # nothing exploded, nothing written
        count = (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one()
    assert count == 0


def test_lifespan_runs_and_cancels_injected_consumer(catalog, identity, saga, db_url):
    """Same contract as identity's convergence loop: the repoint consumer
    task lives and dies with the app."""

    class FakeConsumer:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def run(self):
            self.started = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    fake = FakeConsumer()
    settings = Settings(database_url=db_url, create_all=True)
    app = create_app(settings, catalog=catalog, identity=identity, saga=saga, consumer=fake)  # type: ignore[arg-type]
    with TestClient(app):
        pass
    assert fake.started and fake.cancelled
