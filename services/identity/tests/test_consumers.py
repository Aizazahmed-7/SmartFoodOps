"""Grant-convergence branches (ADR-0020 made real). The loop that feeds
this handler is smartfood_kafka.EventConsumer, tested in its own lib."""

import asyncio
import json

import sqlalchemy as sa
from identity.config import Settings
from identity.consumers import GrantConvergenceHandler
from identity.db import metadata, processed_events, users
from identity.domain.service import IdentityService
from identity.keys import load_or_generate
from smartfood_auth import TokenIssuer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

EMAIL = "owner@example.com"
PASSWORD = "hunter2hunter2"


async def _harness(tmp_path):
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        signing_key_path=str(tmp_path / "key.pem"),
        token_issuer="http://identity.test",
    )
    engine = create_async_engine(
        settings.database_url, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    key = load_or_generate(settings.signing_key_path)
    issuer = TokenIssuer(
        key,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        ttl_seconds=settings.access_ttl_seconds,
    )
    service = IdentityService(
        sessions,
        issuer,
        access_ttl_seconds=settings.access_ttl_seconds,
        refresh_ttl_days=settings.refresh_ttl_days,
    )
    await service.register(email=EMAIL, password=PASSWORD, full_name=None)
    async with sessions() as s:
        user_id = (await s.execute(sa.select(users.c.id))).scalar_one()
    return GrantConvergenceHandler(sessions, service), sessions, user_id


def _event(user_id: str, *, event_id: str = "evt-1", event_type: str = "RestaurantCreated"):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": "restaurant",
        "aggregate_id": "rst_9",
        "aggregate_version": 1,
        "cell_id": "c1",
        "payload": json.dumps({"owner_user_id": user_id, "name": "Biryani House"}),
    }


async def _user_row(sessions):
    async with sessions() as s:
        return (await s.execute(sa.select(users))).one()


async def _processed_count(sessions) -> int:
    async with sessions() as s:
        return (
            await s.execute(sa.select(sa.func.count()).select_from(processed_events))
        ).scalar_one()


async def test_event_promotes_owner_and_marks_processed(tmp_path):
    handler, sessions, user_id = await _harness(tmp_path)
    await handler.handle(_event(user_id))
    user = await _user_row(sessions)
    assert (user.role, user.restaurant_id) == ("restaurant_admin", "rst_9")
    assert await _processed_count(sessions) == 1


async def test_replayed_delivery_is_skipped(tmp_path):
    handler, sessions, user_id = await _harness(tmp_path)
    await handler.handle(_event(user_id))
    await handler.handle(_event(user_id))  # at-least-once redelivery
    assert await _processed_count(sessions) == 1  # dedupe held


async def test_losing_the_mark_race_is_harmless(tmp_path):
    """Two workers pass the seen-check together; the second's processed-mark
    hits the PK and takes the IntegrityError branch — deterministic here
    because both grants are idempotent anyway."""
    handler, sessions, user_id = await _harness(tmp_path)
    await handler._mark_processed("evt-race")
    await handler._mark_processed("evt-race")  # the loser — swallowed
    assert await _processed_count(sessions) == 1


async def test_any_surviving_event_converges_the_grant(tmp_path):
    """THE compaction contract: catalog.changes keeps only the LAST event
    per restaurant — which may be a menu edit, not RestaurantCreated. A
    type-filtered consumer would lose its trigger forever; ours converges
    from whatever event survives, because every payload carries the owner."""
    handler, sessions, user_id = await _harness(tmp_path)
    await handler.handle(_event(user_id, event_type="ItemAdded"))  # the survivor
    user = await _user_row(sessions)
    assert (user.role, user.restaurant_id) == ("restaurant_admin", "rst_9")
    assert await _processed_count(sessions) == 1


async def test_non_restaurant_or_ownerless_events_are_ignored(tmp_path):
    handler, sessions, user_id = await _harness(tmp_path)
    foreign = _event(user_id, event_type="StockAdjusted")
    foreign["aggregate_type"] = "stock"  # another aggregate entirely
    await handler.handle(foreign)
    ownerless = _event(user_id)  # a pre-owner-field replay from history
    ownerless["payload"] = json.dumps({"name": "Biryani House"})
    await handler.handle(ownerless)
    assert await _processed_count(sessions) == 0  # not even marked
    assert (await _user_row(sessions)).role == "customer"


async def test_unappliable_grant_is_marked_not_poisonous(tmp_path):
    handler, sessions, user_id = await _harness(tmp_path)
    async with sessions() as s:  # a rider can't own restaurants → GrantConflict
        await s.execute(users.update().values(role="rider"))
        await s.commit()
    await handler.handle(_event(user_id))
    assert await _processed_count(sessions) == 1  # marked → can't loop forever
    assert (await _user_row(sessions)).role == "rider"  # untouched


async def test_unknown_owner_is_marked_not_poisonous(tmp_path):
    handler, sessions, _ = await _harness(tmp_path)
    await handler.handle(_event("usr_ghost", event_id="evt-ghost"))
    assert await _processed_count(sessions) == 1


# ── app wiring ─────────────────────────────────────────────────────


async def test_lifespan_runs_and_cancels_injected_consumer(settings):
    """Same contract as catalog's poller: the consumer task lives and dies
    with the app."""
    from fastapi.testclient import TestClient
    from identity.main import create_app

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
    with TestClient(create_app(settings, consumer=fake)):  # type: ignore[arg-type]
        pass
    assert fake.started and fake.cancelled


async def test_brand_id_in_payload_wins_over_the_aggregate(tmp_path):
    """A branch event carries its brand_id (ADR-0028): converging from ANY
    surviving compacted event must land the owner on the BRAND, and a later
    branch event must repoint a legacy branch-scoped grant."""
    handler, sessions, user_id = await _harness(tmp_path)
    await handler.handle(_event(user_id))  # legacy event → scoped to rst_9
    branded = _event(user_id, event_id="evt-2")
    branded["payload"] = json.dumps(
        {"owner_user_id": user_id, "name": "Biryani House", "brand_id": "brd_9"}
    )
    await handler.handle(branded)
    user = await _user_row(sessions)
    assert (user.role, user.restaurant_id) == ("restaurant_admin", "brd_9")
    assert await _processed_count(sessions) == 2


async def test_null_brand_id_falls_back_to_the_aggregate(tmp_path):
    """Transitional legacy branches emit brand_id=None — the pre-brands
    behavior must hold exactly (never stamp a null scope)."""
    handler, sessions, user_id = await _harness(tmp_path)
    event = _event(user_id)
    event["payload"] = json.dumps(
        {"owner_user_id": user_id, "name": "Biryani House", "brand_id": None}
    )
    await handler.handle(event)
    user = await _user_row(sessions)
    assert (user.role, user.restaurant_id) == ("restaurant_admin", "rst_9")
