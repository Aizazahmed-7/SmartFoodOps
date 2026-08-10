"""Domain-level tests: the four-write transaction's artifacts, verified in
the database — things no HTTP assertion can see."""

import sqlalchemy as sa
from catalog.db import menu_versions, metadata, outbox
from catalog.domain.service import CatalogService
from smartfood_outbox import event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def _service():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return CatalogService(sessions), sessions


async def test_every_mutation_leaves_the_four_writes():
    svc, sessions = await _service()
    r = await svc.create_restaurant(
        owner_user_id="usr_1", name="Biryani House", city="springfield",
        cuisines=["bbq", "pakistani"], lat=None, lon=None, hours=None,
    )
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
