"""Grant-convergence branches (ADR-0020 made real) + the consumer loop."""

import asyncio
import json
from types import SimpleNamespace

import sqlalchemy as sa
from identity.config import Settings
from identity.consumers import CatalogChangesConsumer, GrantConvergenceHandler
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


async def test_foreign_event_types_are_ignored(tmp_path):
    handler, sessions, user_id = await _harness(tmp_path)
    await handler.handle(_event(user_id, event_type="ItemAdded"))
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


# ── the thin loop ──────────────────────────────────────────────────


class StubKafkaConsumer:
    def __init__(self, values: list[bytes | tuple[bytes, list[tuple[str, bytes]]]]):
        self._values = values
        self.started = self.stopped = False
        self.commits = 0

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def commit(self):
        self.commits += 1

    def __aiter__(self):
        async def gen():
            for value in self._values:
                # bytes, or (bytes, kafka_headers) — mirrors ConsumerRecord,
                # which always has a .headers attribute.
                payload, headers = value if isinstance(value, tuple) else (value, None)
                yield SimpleNamespace(value=payload, headers=headers)

        return gen()


class StubSerde:
    async def decode(self, data: bytes) -> dict:
        return json.loads(data)


class RecordingHandler:
    def __init__(self):
        self.events: list[dict] = []

    async def handle(self, event: dict) -> None:
        self.events.append(event)


async def test_loop_decodes_handles_then_commits():
    client = StubKafkaConsumer([b'{"event_id": "e1"}', b'{"event_id": "e2"}'])
    handler = RecordingHandler()
    await CatalogChangesConsumer(client, StubSerde(), handler).run()
    assert [e["event_id"] for e in handler.events] == ["e1", "e2"]
    assert client.commits == 2  # committed AFTER each handle — at-least-once
    assert client.started and client.stopped  # stop() even on normal exit


async def test_loop_rebinds_trace_context_from_kafka_headers():
    """The consumer's log lines must join the producing request's trace_id —
    valid header binds it, absent/garbage headers bind nothing (and never
    leak the previous message's id)."""
    import structlog

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    client = StubKafkaConsumer(
        [
            (b'{"event_id": "e1"}', [("traceparent", tp.encode())]),
            b'{"event_id": "e2"}',  # no headers at all
            (b'{"event_id": "e3"}', [("traceparent", b"garbage")]),
        ]
    )
    seen: list[str | None] = []

    class ContextProbe:
        async def handle(self, event: dict) -> None:
            seen.append(structlog.contextvars.get_contextvars().get("trace_id"))

    await CatalogChangesConsumer(client, StubSerde(), ContextProbe()).run()
    assert seen == ["ab" * 16, None, None]


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
