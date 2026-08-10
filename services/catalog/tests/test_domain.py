"""Domain-level tests: the four-write transaction's artifacts and the
onboarding race branch — things no HTTP assertion can see."""

import sqlalchemy as sa
from catalog.adapters.repo import CatalogRepo
from catalog.db import menu_versions, metadata, outbox
from catalog.domain.service import CatalogService
from smartfood_outbox import event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def _service(grants):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return CatalogService(sessions, grants), sessions


async def _create(svc, owner="usr_1", name="Biryani House"):
    restaurant, created = await svc.create_restaurant(
        owner_user_id=owner, name=name, city="springfield",
        cuisines=["bbq", "pakistani"], lat=None, lon=None, hours=None,
    )
    return restaurant, created


async def test_every_mutation_leaves_the_four_writes(grants):
    svc, sessions = await _service(grants)
    r, _ = await _create(svc)
    await svc.update_restaurant(r.id, {"name": "Biryani Palace"}, None)
    await svc.set_status(r.id, "paused")

    async with sessions() as s:
        versions = (
            await s.execute(
                sa.select(menu_versions.c.version).order_by(menu_versions.c.version)
            )
        ).scalars().all()
        events = (
            await s.execute(sa.select(outbox).order_by(outbox.c.aggregate_version))
        ).all()

    assert versions == [1, 2, 3]  # audit row per mutation, no gaps
    assert [e.event_type for e in events] == [
        "RestaurantCreated", "RestaurantUpdated", "RestaurantPaused",
    ]
    # Deterministic identity: anyone can recompute the id of a fact.
    assert events[0].id == event_id("restaurant", r.id, 1, "RestaurantCreated")
    assert all(e.published_at is None for e in events)  # staged, drained in W3
    # Snapshots stand alone (compacted topic): each carries full state.
    assert events[0].payload["owner_user_id"] == "usr_1"
    assert events[1].payload["name"] == "Biryani Palace"
    assert events[2].payload["status"] == "paused"
    assert events[2].payload["cuisines"] == ["bbq", "pakistani"]


async def test_concurrent_onboarding_race_adopts_winner(grants, monkeypatch):
    """Two devices POST at once: both pre-checks miss, one INSERT wins the
    UNIQUE(owner_user_id) race, the loser rolls back and adopts the winner."""
    svc, sessions = await _service(grants)
    first, created = await _create(svc)
    assert created

    real = CatalogRepo.get_restaurant_by_owner
    calls = {"n": 0}

    async def racy(self, owner_user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the race window: pre-check misses the winner
        return await real(self, owner_user_id)  # recovery lookup sees it

    monkeypatch.setattr(CatalogRepo, "get_restaurant_by_owner", racy)
    second, created2 = await _create(svc, name="Duplicate Attempt")

    assert created2 is False
    assert second.id == first.id  # adopted, not duplicated
    async with sessions() as s:
        count = (
            await s.execute(sa.select(sa.func.count()).select_from(outbox))
        ).scalar_one()
    assert count == 1  # the losing attempt's writes all rolled back


async def test_event_ids_are_deterministic_and_distinct():
    assert event_id("restaurant", "rst_1", 1, "RestaurantCreated") == event_id(
        "restaurant", "rst_1", 1, "RestaurantCreated"
    )
    ids = {
        event_id("restaurant", "rst_1", 1, "RestaurantCreated"),
        event_id("restaurant", "rst_1", 2, "RestaurantCreated"),
        event_id("restaurant", "rst_2", 1, "RestaurantCreated"),
        event_id("restaurant", "rst_1", 1, "RestaurantPaused"),
        event_id("order", "rst_1", 1, "RestaurantCreated"),
    }
    assert len(ids) == 5
