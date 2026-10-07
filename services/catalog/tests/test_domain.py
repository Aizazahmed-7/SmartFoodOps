"""Domain-level tests: the four-write transaction's artifacts and the
onboarding race branch — things no HTTP assertion can see."""

import sqlalchemy as sa
from catalog.adapters.repo import CatalogRepo
from catalog.db import metadata, outbox
from catalog.domain.service import CatalogService
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


async def test_every_mutation_leaves_the_three_writes(grants, cache):
    svc, sessions = await _service(grants, cache)
    r, _ = await _create(svc)
    await svc.update_restaurant(r.id, {"name": "Biryani Palace"}, None)
    branch_id = (await svc.list_branches(r.id))[0].id
    # Pause is a BRANCH action since 0009 — a brand has no status to set.
    await svc.set_status(branch_id, "paused")

    async with sessions() as s:
        rows = (await s.execute(sa.select(outbox).order_by(outbox.c.occurred_at))).all()

    # Every BRAND mutation fans out (ADR-0028); a branch mutation does not.
    # So the brand sees two events and the branch sees three: its two
    # inherited from the fan-out plus its own pause.
    events = [e for e in rows if e.aggregate_id == r.id]
    branch_events = [e for e in rows if e.aggregate_id != r.id]
    # Ordered by occurred_at: each mutation is its own transaction with its
    # own clock, so the sequence is well defined without a version column
    # (ADR-0038). One event per mutation per aggregate, no gaps.
    assert [e.event_type for e in events] == ["RestaurantCreated", "RestaurantUpdated"]
    assert [e.event_type for e in branch_events] == [
        "RestaurantCreated",
        "RestaurantUpdated",
        "RestaurantPaused",
    ]
    assert all(e.payload["brand_id"] == r.id for e in branch_events)
    assert branch_events[1].payload["name"] == "Biryani Palace"  # the copy propagated
    # The pause landed on the branch's payload and nowhere else.
    assert branch_events[2].payload["status"] == "paused"
    assert all(e.payload["status"] is None for e in events)  # a brand has none
    assert (events[0].aggregate_type, events[0].event_type) == (
        "restaurant",
        "RestaurantCreated",
    )
    assert all(e.published_at is None for e in rows)  # staged, drained in W3
    # Snapshots stand alone (compacted topic): each carries full state —
    # INCLUDING the owner on EVERY event, not just the birth one. Identity's
    # grant convergence must survive RestaurantCreated being compacted away
    # in favor of any later event on the same key.
    assert all(e.payload["owner_user_id"] == "usr_1" for e in rows)
    assert events[1].payload["name"] == "Biryani Palace"
    # Full state on the LAST event of each aggregate — the one compaction
    # keeps. Cuisines ride on both; status only exists on the branch's.
    assert events[-1].payload["cuisines"] == ["bbq", "pakistani"]
    assert branch_events[-1].payload["cuisines"] == ["bbq", "pakistani"]


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
    assert count == 2  # brand + first-branch events; the loser's writes all rolled back


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
    # A PROFILE event after menu edits. It has to be the BRANCH's pause
    # since 0009, and the branch is also where the base menu is inherited —
    # so this still proves a profile event carries the whole menu.
    branch_id = (await svc.list_branches(r.id))[0].id
    await svc.set_status(branch_id, "paused")

    async with sessions() as s:
        rows = (await s.execute(sa.select(outbox).order_by(outbox.c.occurred_at))).all()

    events = [e for e in rows if e.aggregate_id == branch_id]
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
    # The branch twins carry the same base items as their EFFECTIVE menu —
    # what inventory provisions from, never having heard of inheritance.
    branch_last = [e for e in rows if e.aggregate_id != r.id][-1].payload
    branch_item = branch_last["menu"]["categories"][0]["items"][0]
    assert (branch_item["name"], branch_item["source"]) == ("Biryani", "base")


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


async def test_the_render_opens_its_snapshot_before_any_read(grants, cache, monkeypatch):
    """`begin_snapshot` must be the FIRST statement in the transaction.
    Postgres cannot change the isolation level once a statement has run, so
    a query added above it would silently downgrade the whole render to READ
    COMMITTED — and nothing else would notice (ADR-0037)."""
    svc, _ = await _service(grants, cache)
    r, _ = await _create(svc)

    trace: list[str] = []
    real_snapshot = CatalogRepo.begin_snapshot
    real_restaurant = CatalogRepo.get_restaurant
    real_menu_rows = CatalogRepo.get_menu_rows

    async def snap(self):
        trace.append("snapshot")
        return await real_snapshot(self)

    async def restaurant(self, restaurant_id):
        trace.append("read")
        return await real_restaurant(self, restaurant_id)

    async def menu_rows(self, scope_ids):
        trace.append("read")
        return await real_menu_rows(self, scope_ids)

    monkeypatch.setattr(CatalogRepo, "begin_snapshot", snap)
    monkeypatch.setattr(CatalogRepo, "get_restaurant", restaurant)
    monkeypatch.setattr(CatalogRepo, "get_menu_rows", menu_rows)
    await svc.get_menu(r.id)
    assert trace[0] == "snapshot", trace
    assert trace.count("snapshot") == 1  # one transaction, one snapshot
    assert "read" in trace  # and it really did read inside it


async def test_the_pricing_read_opens_its_snapshot_before_any_read(grants, cache, monkeypatch):
    """Same ordering rule on the money path, where a torn read would price
    one line from the old menu and another from the new."""
    svc, _ = await _service(grants, cache)
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

    trace: list[str] = []
    real_snapshot = CatalogRepo.begin_snapshot
    real_pricing = CatalogRepo.get_pricing_rows

    async def snap(self):
        trace.append("snapshot")
        return await real_snapshot(self)

    async def pricing(self, scope_ids, item_ids):
        trace.append("read")
        return await real_pricing(self, scope_ids, item_ids)

    monkeypatch.setattr(CatalogRepo, "begin_snapshot", snap)
    monkeypatch.setattr(CatalogRepo, "get_pricing_rows", pricing)
    body = await svc.pricing_read(r.id, [item["id"]])
    assert trace == ["snapshot", "read"], trace
    assert body["items"][0]["price_cents"] == 1200


async def test_begin_snapshot_asks_postgres_for_repeatable_read():
    """The Postgres arm, unreachable from the sqlite suite (which needs no
    isolation change — one connection, no interleaving writer). Asserts the
    exact level: a valid-but-weaker one would be accepted silently and only
    surface as a torn read in production."""
    from typing import Any, cast

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _Session:
        bind = _Bind()

        def __init__(self) -> None:
            self.captured: dict[str, Any] | None = None

        async def connection(self, execution_options=None):
            self.captured = execution_options

    session = _Session()
    await CatalogRepo(cast(Any, session)).begin_snapshot()
    assert session.captured == {"isolation_level": "REPEATABLE READ"}


async def test_singleflight_loser_adopts_winners_menu(grants):
    """Lock lost → wait a beat → the winner's menu appeared → serve it,
    touching neither the DB nor the winner's lock."""
    import json

    doc = {"restaurant_id": "rst_x", "version": 3, "categories": []}

    class WinnerAppears:
        def __init__(self):
            self.reads = 0

        async def get(self, key: str) -> str | None:
            self.reads += 1
            return None if self.reads == 1 else json.dumps(doc)  # appears after the wait

        async def set(self, key, value, ttl_seconds): ...
        async def delete(self, key): ...
        async def acquire_lock(self, key, ttl_ms) -> bool:
            return False  # someone else is rendering

        async def release_lock(self, key): ...

    # sessions=None proves the DB is never touched on this path.
    from typing import Any, cast

    svc = CatalogService(cast(Any, None), grants, WinnerAppears(), _NullSearch())
    assert await svc.get_menu("rst_x") == doc


async def test_staged_events_carry_the_traceparent(grants, cache):
    """The async hop stays stitched: whatever traceparent the middleware set
    for the request lands on the outbox row (docs §12)."""
    from smartfood_otel.propagation import use_traceparent

    svc, sessions = await _service(grants, cache)
    tp = "00-" + "12" * 16 + "-" + "34" * 8 + "-01"
    with use_traceparent(tp):
        r, _ = await _create(svc)
        async with sessions() as s:
            rows = (await s.execute(sa.select(outbox))).all()
        assert rows and all(row.traceparent == tp for row in rows)  # brand + branch alike
