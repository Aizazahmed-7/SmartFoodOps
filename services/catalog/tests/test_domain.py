"""Domain-level tests: the four-write transaction's artifacts and the
onboarding race branch — things no HTTP assertion can see."""

import sqlalchemy as sa
from catalog.adapters.repo import CatalogRepo
from catalog.db import menu_versions, metadata, outbox
from catalog.domain.service import CatalogService
from smartfood_outbox import event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class _NullSearch:
    async def search(self, **kwargs) -> list[dict]:
        return []


async def _service(grants, cache):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return CatalogService(sessions, grants, cache, _NullSearch()), sessions


async def _create(svc, owner="usr_1", name="Biryani House"):
    restaurant, created = await svc.create_restaurant(
        owner_user_id=owner,
        name=name,
        city="springfield",
        cuisines=["bbq", "pakistani"],
        lat=None,
        lon=None,
        hours=None,
    )
    return restaurant, created


async def test_every_mutation_leaves_the_four_writes(grants, cache):
    svc, sessions = await _service(grants, cache)
    r, _ = await _create(svc)
    await svc.update_restaurant(r.id, {"name": "Biryani Palace"}, None)
    await svc.set_status(r.id, "paused")

    async with sessions() as s:
        versions = (
            (await s.execute(sa.select(menu_versions.c.version).order_by(menu_versions.c.version)))
            .scalars()
            .all()
        )
        events = (await s.execute(sa.select(outbox).order_by(outbox.c.aggregate_version))).all()

    assert versions == [1, 2, 3]  # audit row per mutation, no gaps
    assert [e.event_type for e in events] == [
        "RestaurantCreated",
        "RestaurantUpdated",
        "RestaurantPaused",
    ]
    # Deterministic identity: anyone can recompute the id of a fact.
    assert events[0].id == event_id("restaurant", r.id, 1, "RestaurantCreated")
    assert all(e.published_at is None for e in events)  # staged, drained in W3
    # Snapshots stand alone (compacted topic): each carries full state —
    # INCLUDING the owner on EVERY event, not just the birth one. Identity's
    # grant convergence must survive RestaurantCreated being compacted away
    # in favor of any later event on the same key.
    assert all(e.payload["owner_user_id"] == "usr_1" for e in events)
    assert events[1].payload["name"] == "Biryani Palace"
    assert events[2].payload["status"] == "paused"
    assert events[2].payload["cuisines"] == ["bbq", "pakistani"]


async def test_concurrent_onboarding_race_adopts_winner(grants, cache, monkeypatch):
    """Two devices POST at once: both pre-checks miss, one INSERT wins the
    UNIQUE(owner_user_id) race, the loser rolls back and adopts the winner."""
    svc, sessions = await _service(grants, cache)
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
        count = (await s.execute(sa.select(sa.func.count()).select_from(outbox))).scalar_one()
    assert count == 1  # the losing attempt's writes all rolled back


async def test_every_event_carries_full_state(grants, cache):
    """Compaction safety: catalog.changes keeps only the LAST event per
    restaurant, so even a profile event must carry the whole menu."""
    svc, sessions = await _service(grants, cache)
    r, _ = await _create(svc)
    cat = await svc.add_category(r.id, name="Mains", rank=0)
    await svc.add_item(
        r.id,
        category_id=cat["id"],
        fields={
            "name": "Biryani",
            "description": None,
            "price_cents": 1200,
            "currency": "USD",
            "available": True,
            "rank": 0,
        },
        tags=["halal"],
        modifier_groups=[
            {
                "name": "Size",
                "min_select": 1,
                "max_select": 1,
                "rank": 0,
                "options": [{"name": "Family", "price_delta_cents": 600, "rank": 0}],
            }
        ],
    )
    await svc.set_status(r.id, "paused")  # a PROFILE event, after menu edits

    async with sessions() as s:
        events = (await s.execute(sa.select(outbox).order_by(outbox.c.aggregate_version))).all()

    assert [e.event_type for e in events] == [
        "RestaurantCreated",
        "CategoryAdded",
        "ItemAdded",
        "RestaurantPaused",
    ]
    last = events[-1].payload  # the only event compaction guarantees survives
    assert last["status"] == "paused"
    item = last["menu"]["categories"][0]["items"][0]
    assert item["name"] == "Biryani"
    assert item["tags"] == ["halal"]
    assert item["modifier_groups"][0]["options"][0]["price_delta_cents"] == 600
    # And the snapshot inside each event matches its OWN moment: the create
    # event has an empty menu — state as of that commit, not as of now.
    assert events[0].payload["menu"] == {"categories": []}


async def test_delete_item_leaves_no_orphan_rows(grants, cache):
    from catalog.db import item_tags, modifier_groups, modifier_options

    svc, sessions = await _service(grants, cache)
    r, _ = await _create(svc)
    cat = await svc.add_category(r.id, name="Mains", rank=0)
    item = await svc.add_item(
        r.id,
        category_id=cat["id"],
        fields={
            "name": "Biryani",
            "description": None,
            "price_cents": 1200,
            "currency": "USD",
            "available": True,
            "rank": 0,
        },
        tags=["halal", "spicy"],
        modifier_groups=[
            {
                "name": "Size",
                "min_select": 0,
                "max_select": 1,
                "rank": 0,
                "options": [
                    {"name": "A", "price_delta_cents": 0, "rank": 0},
                    {"name": "B", "price_delta_cents": 1, "rank": 1},
                ],
            }
        ],
    )
    await svc.delete_item(r.id, item["id"])

    async with sessions() as s:
        for table in (item_tags, modifier_groups, modifier_options):
            count = (await s.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()
            assert count == 0  # children die with the item — no orphans


async def test_render_retries_on_torn_read(grants, cache, monkeypatch):
    """READ COMMITTED can tear: version read at N, rows read at N+1. Caching
    that under version N would poison an immutable key — the renderer must
    detect the move and re-read."""
    from catalog.db import restaurants

    svc, sessions = await _service(grants, cache)
    r, _ = await _create(svc)

    real = CatalogRepo.get_menu_rows
    calls = {"n": 0}

    async def tearing(self, restaurant_id):
        calls["n"] += 1
        rows = await real(self, restaurant_id)
        if calls["n"] == 1:  # a concurrent edit lands mid-read
            await self._s.execute(restaurants.update().values(version=restaurants.c.version + 1))
        return rows

    monkeypatch.setattr(CatalogRepo, "get_menu_rows", tearing)
    menu = await svc.get_menu(r.id)
    assert calls["n"] == 2  # first pass torn → re-rendered
    assert menu["version"] == r.version + 1  # served (and cached) post-edit state


async def test_pricing_read_retries_on_torn_read(grants, cache, monkeypatch):
    """Same tear hazard as the renderer, higher stakes: a price edit landing
    mid-read must not hand pricing a mixed-version view."""
    from catalog.db import restaurants

    svc, sessions = await _service(grants, cache)
    r, _ = await _create(svc)
    cat = await svc.add_category(r.id, name="Mains", rank=0)
    item = await svc.add_item(
        r.id,
        category_id=cat["id"],
        fields={
            "name": "Biryani",
            "description": None,
            "price_cents": 1200,
            "currency": "USD",
            "available": True,
            "rank": 0,
        },
        tags=[],
        modifier_groups=[],
    )

    real = CatalogRepo.get_pricing_rows
    calls = {"n": 0}

    async def tearing(self, restaurant_id, item_ids):
        calls["n"] += 1
        rows = await real(self, restaurant_id, item_ids)
        if calls["n"] == 1:  # concurrent price edit mid-read
            await self._s.execute(restaurants.update().values(version=restaurants.c.version + 1))
        return rows

    monkeypatch.setattr(CatalogRepo, "get_pricing_rows", tearing)
    body = await svc.pricing_read(r.id, [item["id"]])
    assert calls["n"] == 2  # re-read after the version moved
    assert body["restaurant"]["version"] == item["version"] + 1


async def test_singleflight_loser_adopts_winners_blob(grants):
    """Lock lost → wait a beat → the winner's pointer+blob appeared → serve
    them, touching neither the DB nor the winner's lock."""
    import json

    doc = {"restaurant_id": "rst_x", "version": 3, "categories": []}

    class WinnerAppears:
        def __init__(self):
            self.ptr_reads = 0

        async def get(self, key: str) -> str | None:
            if "ptr" in key:
                self.ptr_reads += 1
                return None if self.ptr_reads == 1 else "3"  # appears after the wait
            return json.dumps(doc)

        async def set(self, key, value, ttl_seconds): ...
        async def delete(self, key): ...
        async def acquire_lock(self, key, ttl_ms) -> bool:
            return False  # someone else is rendering

        async def release_lock(self, key): ...

    # sessions=None proves the DB is never touched on this path.
    from typing import Any, cast

    svc = CatalogService(cast(Any, None), grants, WinnerAppears(), _NullSearch())
    assert await svc.get_menu("rst_x") == doc


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


async def test_staged_events_carry_the_traceparent(grants, cache):
    """The async hop stays stitched: whatever traceparent the middleware set
    for the request lands on the outbox row (docs §12)."""
    from smartfood_otel.propagation import set_current_traceparent

    svc, sessions = await _service(grants, cache)
    tp = "00-" + "12" * 16 + "-" + "34" * 8 + "-01"
    set_current_traceparent(tp)
    r, _ = await _create(svc)
    async with sessions() as s:
        row = (await s.execute(sa.select(outbox))).one()
    assert row.traceparent == tp
