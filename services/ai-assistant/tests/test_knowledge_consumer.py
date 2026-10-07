"""`assistant.knowledge.v1` against real sqlite (FR-57, DoD-2).

The debounce is the part worth testing hard, because both of its failure
modes are silent. Too eager and the ADR-0028 fan-out bills us twelve times
for one edit; too lazy — a sliding window — and a restaurant under
continuous editing never re-indexes at all, while the queue looks healthy
the whole time.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from ai_assistant.consumers import GROUP_KNOWLEDGE, KnowledgeHandler
from ai_assistant.db import knowledge_pending, metadata
from smartfood_kafka import EventConsumer
from smartfood_kafka.testing import StubDlq, StubKafkaConsumer, StubMessage, StubSerde
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
DEBOUNCE = 30.0

BRANCH = {
    "kind": "branch",
    "name": "Biryani House",
    "branch_label": "Downtown",
    "city": "springfield",
    "cuisines": ["pakistani"],
    "status": "open",
    "menu": {"categories": []},
}


def _event(restaurant_id: str = "r1", payload: dict | None = None, **over):
    return {
        "aggregate_type": "restaurant",
        "aggregate_id": restaurant_id,
        "event_type": "ItemUpdated",
        "payload": json.dumps(payload if payload is not None else BRANCH),
        **over,
    }


class Clock:
    """A hand-wound clock: the whole point of these tests is what happens
    across a window, and a real one would make that either slow or flaky."""

    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def handler(sessions, clock):
    return KnowledgeHandler(sessions, debounce_s=DEBOUNCE, clock=clock)


async def _rows(sessions):
    async with sessions() as session:
        result = await session.execute(sa.select(knowledge_pending))
        return result.mappings().all()


async def _one(sessions):
    rows = await _rows(sessions)
    assert len(rows) == 1
    return rows[0]


def _at(row, column: str) -> datetime:
    """sqlite stores no offset, so it hands back a naive datetime where
    Postgres returns an aware one. Re-stamping UTC on read is the same
    idiom notification's consumer tests use; the column is
    `TIMESTAMP(timezone=True)` and everything written to it is UTC."""
    return row[column].replace(tzinfo=UTC)


# ── staging ─────────────────────────────────────────────────────────


async def test_a_menu_change_is_queued_with_a_due_time(handler, sessions):
    await handler.handle(_event())
    row = await _one(sessions)
    assert row["restaurant_id"] == "r1"
    assert _at(row, "due_at") == T0 + timedelta(seconds=DEBOUNCE)
    assert _at(row, "first_seen_at") == T0
    assert row["payload"]["name"] == "Biryani House"


async def test_the_whole_payload_is_staged_not_a_pointer(handler, sessions):
    """Catalog's events are full-state snapshots, so the drain needs no
    catalog call — and cannot read a state NEWER than the event it is
    acting on, which is what keeps a replay deterministic."""
    await handler.handle(_event())
    assert (await _one(sessions))["payload"] == BRANCH


# ── DoD-2: duplicate delivery ───────────────────────────────────────


async def test_duplicate_delivery_queues_one_pass_not_two(handler, sessions, clock):
    """NATURAL_KEY dedupe: the pending row is keyed by restaurant_id, so an
    at-least-once redelivery upserts it rather than queueing a second pass.
    No processed_events ledger, and none wanted — a full-state payload makes
    "apply twice" and "apply once" indistinguishable."""
    event = _event()
    await handler.handle(event)
    clock.advance(5)
    await handler.handle(event)
    row = await _one(sessions)
    assert _at(row, "due_at") == T0 + timedelta(seconds=DEBOUNCE)


async def test_redelivery_through_the_real_consumer_loop(sessions, clock):
    """The same property one layer up, through the actual runtime: each
    __aiter__ replays from the start, which is exactly what an uncommitted
    offset does on a rejoin."""
    handler = KnowledgeHandler(sessions, debounce_s=DEBOUNCE, clock=clock)
    message = StubMessage(value=json.dumps(_event()).encode(), topic="c1.catalog.changes")
    await EventConsumer(
        "c1.catalog.changes",
        GROUP_KNOWLEDGE,
        handler,
        StubSerde(),
        client=StubKafkaConsumer([message, message]),
        dlq=StubDlq(),
    ).consume_once()
    assert len(await _rows(sessions)) == 1


# ── DoD-2: poison message ───────────────────────────────────────────


async def test_an_unparseable_payload_parks_rather_than_vanishing(sessions, clock):
    """A menu that cannot be read must end up somewhere a human can look.
    Swallowing it would leave a restaurant silently missing from search with
    nothing to point at; raising invokes bounded retry then the DLQ, where
    the ORIGINAL bytes are replayable (ADR-0021)."""
    handler = KnowledgeHandler(sessions, debounce_s=DEBOUNCE, clock=clock)
    broken = json.dumps({**_event(), "payload": "{not json"}).encode()
    dlq = StubDlq()
    await EventConsumer(
        "c1.catalog.changes",
        GROUP_KNOWLEDGE,
        handler,
        StubSerde(),
        client=StubKafkaConsumer([StubMessage(value=broken, topic="c1.catalog.changes")]),
        dlq=dlq,
        max_attempts=2,
        backoff_seconds=0.0,
    ).consume_once()
    topic, parked, _, _headers = dlq.parked[0]
    assert topic == "c1.catalog.changes.dlq"
    assert parked == broken  # byte-identical, so a replay is a re-produce
    assert await _rows(sessions) == []


async def test_the_partition_keeps_moving_past_a_poison_message(sessions, clock):
    """A poison message costs seconds, not a partition: the good event
    behind it still lands."""
    handler = KnowledgeHandler(sessions, debounce_s=DEBOUNCE, clock=clock)
    messages = [
        StubMessage(value=json.dumps({**_event(), "payload": "{nope"}).encode()),
        StubMessage(value=json.dumps(_event("r2")).encode()),
    ]
    await EventConsumer(
        "c1.catalog.changes",
        GROUP_KNOWLEDGE,
        handler,
        StubSerde(),
        client=StubKafkaConsumer(messages),
        dlq=StubDlq(),
        max_attempts=2,
        backoff_seconds=0.0,
    ).consume_once()
    assert [r["restaurant_id"] for r in await _rows(sessions)] == ["r2"]


# ── the window: fixed, not sliding ──────────────────────────────────


async def test_a_later_edit_keeps_the_earlier_deadline(handler, sessions, clock):
    """THE test for a fixed window. A second edit 20 s in must not push the
    deadline out to 50 s — a sliding window would, and an owner editing
    twenty dishes over five minutes would never be indexed at all while the
    queue reported itself healthy."""
    await handler.handle(_event())
    clock.advance(20)
    await handler.handle(_event(payload={**BRANCH, "name": "Biryani Palace"}))
    row = await _one(sessions)
    assert _at(row, "due_at") == T0 + timedelta(seconds=DEBOUNCE)


async def test_the_latest_payload_wins_within_the_window(handler, sessions, clock):
    """Full-state snapshots supersede completely — there is no merge to do."""
    await handler.handle(_event())
    clock.advance(20)
    await handler.handle(_event(payload={**BRANCH, "name": "Biryani Palace"}))
    assert (await _one(sessions))["payload"]["name"] == "Biryani Palace"


async def test_first_seen_at_records_the_start_of_the_wait(handler, sessions, clock):
    """So "this restaurant has been waiting four minutes" is answerable
    rather than inferred."""
    await handler.handle(_event())
    clock.advance(20)
    await handler.handle(_event())
    assert _at(await _one(sessions), "first_seen_at") == T0


async def test_an_overdue_row_stays_overdue(handler, sessions, clock):
    """A backlogged row keeps its ORIGINAL deadline when a later edit lands,
    rather than being pushed another window into the future.

    This is the fixed window doing its real job. The row is already late —
    the drain has not reached it — so the correct response to more activity
    is "index this as soon as possible", not "wait another 30 s". Under
    sustained editing the sliding alternative compounds: every edit renews
    the reprieve, and the restaurant is never indexed at all."""
    await handler.handle(_event())
    clock.advance(100)
    await handler.handle(_event())
    assert _at(await _one(sessions), "due_at") == T0 + timedelta(seconds=DEBOUNCE)


async def test_each_restaurant_debounces_independently(handler, sessions, clock):
    """The ADR-0028 fan-out in miniature: one base-menu edit arrives as one
    event per BRANCH, and each branch is its own index entry."""
    for branch in ("r1", "r2", "r3"):
        await handler.handle(_event(branch))
    assert sorted(r["restaurant_id"] for r in await _rows(sessions)) == ["r1", "r2", "r3"]


# ── what never reaches the queue ────────────────────────────────────


async def test_brand_events_are_not_queued(handler, sessions):
    """Queueing work the drain would only discard would make the backlog lie
    about how stale the index is."""
    await handler.handle(_event("b1", {**BRANCH, "kind": "brand", "city": None}))
    assert await _rows(sessions) == []


async def test_a_branch_without_a_city_is_not_queued(handler, sessions):
    await handler.handle(_event(payload={**BRANCH, "city": None}))
    assert await _rows(sessions) == []


async def test_events_from_other_aggregates_are_ignored(handler, sessions):
    await handler.handle(_event(aggregate_type="order"))
    assert await _rows(sessions) == []


# ── DoD-2 paperwork ─────────────────────────────────────────────────


def test_consumer_group_follows_the_naming_rule():
    """`{service}.{purpose}.v{n}`. Re-consuming from the beginning means
    bumping the v — never resetting a live group's offsets — and that is
    exactly how FR-59's rebuild is performed."""
    assert GROUP_KNOWLEDGE == "assistant.knowledge.v1"


async def test_the_default_clock_is_utc_aware(sessions):
    """Every test above injects a clock, so the production default would
    otherwise go unexercised — and a naive `datetime.now()` here would write
    local-time deadlines into a tz-aware column, making a drain in one
    timezone act hours early or late."""
    await KnowledgeHandler(sessions, debounce_s=DEBOUNCE).handle(_event())
    staged = _at(await _one(sessions), "first_seen_at")
    assert abs((staged - datetime.now(UTC)).total_seconds()) < 60
