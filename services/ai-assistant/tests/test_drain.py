"""The drain, against real sqlite with a counting embedder (FR-58, FR-59).

Almost every test here is about how often `embed()` is called, because that
is the one operation in this pipeline that costs money and the one whose
failure mode is invisible: an index that is perfectly correct and four times
more expensive than it needs to be looks exactly like an index that is not.

The other half is the delete leg. Catalog sends no tombstones, so a removed
dish is an ABSENCE — and a chunk left behind is the assistant recommending
something nobody can order.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.embeddings_fake import FakeEmbeddings
from ai_assistant.adapters.repo import PendingRepo
from ai_assistant.consumers import KnowledgeHandler
from ai_assistant.db import item_chunks, knowledge_pending, metadata, restaurant_chunks
from ai_assistant.drain import KnowledgeDrain
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
DIMENSIONS = 8  # a toy width; nothing here depends on 512


def _menu(*items):
    return {
        "categories": [{"id": "c1", "name": "Mains", "items": list(items)}],
    }


def _item(item_id="i1", name="Chicken Karahi", **over):
    return {
        "id": item_id,
        "name": name,
        "description": "Wok-cooked.",
        "price_cents": 899,
        "available": True,
        "tags": ["spicy"],
        **over,
    }


def _payload(**over):
    return {
        "kind": "branch",
        "name": "Biryani House",
        "branch_label": "Downtown",
        "city": "springfield",
        "cuisines": ["pakistani"],
        "status": "open",
        "menu": _menu(_item()),
        **over,
    }


class CountingEmbeddings(FakeEmbeddings):
    """The real fake, wrapped so tests can assert on provider traffic —
    calls (rate-limit pressure) and texts (token cost) separately."""

    def __init__(self):
        super().__init__(dimensions=DIMENSIONS)
        self.calls = 0
        self.texts: list[str] = []

    async def embed(self, texts):
        self.calls += 1
        self.texts.extend(texts)
        return await super().embed(texts)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
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
def embeddings():
    return CountingEmbeddings()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def drain(sessions, embeddings, clock):
    return KnowledgeDrain(sessions, embeddings, interval_s=0.0, batch=20, clock=clock)


async def _stage(sessions, restaurant_id="r1", payload=None, now=T0):
    """Through the real handler, so the tests exercise the same staging path
    production does."""
    await KnowledgeHandler(sessions, debounce_s=30.0, clock=lambda: now).handle(
        {
            "aggregate_type": "restaurant",
            "aggregate_id": restaurant_id,
            "event_type": "ItemUpdated",
            "payload": json.dumps(payload or _payload()),
        }
    )


async def _rows(sessions, table):
    async with sessions() as session:
        return (await session.execute(sa.select(table))).mappings().all()


# ── the basic pass ──────────────────────────────────────────────────


async def test_a_due_restaurant_is_indexed(drain, sessions, clock):
    await _stage(sessions)
    clock.advance(31)
    assert await drain.tick() == 1
    items = await _rows(sessions, item_chunks)
    restaurants = await _rows(sessions, restaurant_chunks)
    assert [r["id"] for r in items] == ["r1:i1"]
    assert [r["id"] for r in restaurants] == ["r1:_self"]
    assert len(items[0]["embedding"]) == DIMENSIONS


async def test_a_restaurant_is_not_touched_before_its_window_closes(drain, sessions, clock):
    await _stage(sessions)
    clock.advance(29)
    assert await drain.tick() == 0
    assert await _rows(sessions, item_chunks) == []


async def test_the_queue_is_cleared_once_indexed(drain, sessions, clock):
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    assert await _rows(sessions, knowledge_pending) == []


async def test_volatile_columns_land_as_filters_not_prose(drain, sessions, clock):
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    row = (await _rows(sessions, item_chunks))[0]
    assert (row["price_cents"], row["available"], row["status"]) == (899, True, "open")
    assert list(row["tags"]) == ["spicy"]
    assert "899" not in row["content"]


# ── what does NOT cost a provider call ──────────────────────────────


async def test_one_call_carries_every_changed_text(drain, sessions, clock, embeddings):
    """Batched by contract: per-text calls are the difference between one
    request and thirty at ingestion volume, and rate limits are per
    request."""
    await _stage(sessions, payload=_payload(menu=_menu(_item("i1"), _item("i2", "Lamb"))))
    clock.advance(31)
    await drain.tick()
    assert embeddings.calls == 1
    assert len(embeddings.texts) == 3  # two dishes + the restaurant chunk


async def test_a_redelivered_payload_embeds_nothing(drain, sessions, clock, embeddings):
    """FR-59 in miniature. The same menu arriving twice must cost nothing
    the second time, or a replay of the topic would be a bill."""
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    before = embeddings.calls
    await _stage(sessions, now=clock.now)
    clock.advance(31)
    await drain.tick()
    assert embeddings.calls == before


async def test_a_price_change_rewrites_the_row_and_embeds_nothing(
    drain, sessions, clock, embeddings
):
    """The routine case — a kitchen changing a price all afternoon — and the
    whole reason price is a column rather than prose."""
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    before = embeddings.calls

    await _stage(sessions, payload=_payload(menu=_menu(_item(price_cents=1299))), now=clock.now)
    clock.advance(31)
    await drain.tick()

    row = (await _rows(sessions, item_chunks))[0]
    assert row["price_cents"] == 1299  # the column moved
    assert embeddings.calls == before  # the provider was not called


async def test_an_86d_dish_keeps_its_vector(drain, sessions, clock, embeddings):
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    vector_before = (await _rows(sessions, item_chunks))[0]["embedding"]
    calls_before = embeddings.calls

    await _stage(sessions, payload=_payload(menu=_menu(_item(available=False))), now=clock.now)
    clock.advance(31)
    await drain.tick()

    row = (await _rows(sessions, item_chunks))[0]
    assert row["available"] is False
    assert list(row["embedding"]) == list(vector_before)
    assert embeddings.calls == calls_before


async def test_a_real_edit_does_embed(drain, sessions, clock, embeddings):
    """The control. Without it every assertion above is satisfied by a drain
    that never calls the provider at all."""
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    before = embeddings.calls

    await _stage(
        sessions, payload=_payload(menu=_menu(_item(description="Now with chilli."))), now=clock.now
    )
    clock.advance(31)
    await drain.tick()
    assert embeddings.calls == before + 1


async def test_a_shared_dish_across_branches_has_identical_text(drain, sessions, clock, embeddings):
    """THE fan-out test. ADR-0028 sends one base-menu edit as a full-state
    event per branch, and item chunks omit the restaurant name precisely so
    the text is byte-identical across all of them.

    Cross-restaurant vector reuse was removed deliberately: the three-dict
    hot path it required (stale / borrowed / missing) cost more in
    readability than it saved, and it was the only thing forcing the drain
    loop to stay sequential. So each branch now buys its own vector for the
    same sentence — the identical text below is what a future reuse pass
    would key on if that trade is ever revisited.
    """
    await _stage(sessions, "r1", _payload(branch_label="Downtown"))
    await _stage(sessions, "r2", _payload(branch_label="Airport"))
    clock.advance(31)
    await drain.tick()

    dishes = [r for r in await _rows(sessions, item_chunks)]
    assert len(dishes) == 2
    # Still byte-identical, and still the same vector — an embedding is a
    # pure function of (text, model), so paying twice buys the same answer.
    assert dishes[0]["content"] == dishes[1]["content"]
    assert list(dishes[0]["embedding"]) == list(dishes[1]["embedding"])
    dish = "Chicken Karahi\nWok-cooked.\nCategory: Mains\nTags: spicy\nCuisines: pakistani"
    # The dish TWICE now — once per branch — plus each branch's own
    # restaurant chunk, whose labels genuinely differ.
    assert sorted(embeddings.texts) == sorted(
        [
            dish,
            dish,
            "Biryani House — Downtown\nCuisines: pakistani",
            "Biryani House — Airport\nCuisines: pakistani",
        ]
    )


async def test_a_dish_repeated_within_one_menu_is_embedded_once(drain, sessions, clock, embeddings):
    """Same text under two ids — a drink listed in two categories."""
    await _stage(sessions, payload=_payload(menu=_menu(_item("i1"), _item("i2"))))
    clock.advance(31)
    await drain.tick()
    assert len(await _rows(sessions, item_chunks)) == 2
    assert len(embeddings.texts) == 2  # the shared dish once + the restaurant


# ── the delete leg ──────────────────────────────────────────────────


async def test_a_removed_dish_is_reconciled_away(drain, sessions, clock):
    """No tombstone arrives — the dish is simply absent from the next
    snapshot. A chunk left behind is a dish the assistant will recommend and
    nobody can order."""
    await _stage(sessions, payload=_payload(menu=_menu(_item("i1"), _item("i2", "Lamb"))))
    clock.advance(31)
    await drain.tick()
    assert len(await _rows(sessions, item_chunks)) == 2

    await _stage(sessions, payload=_payload(menu=_menu(_item("i1"))), now=clock.now)
    clock.advance(31)
    await drain.tick()
    assert [r["id"] for r in await _rows(sessions, item_chunks)] == ["r1:i1"]


async def test_emptying_a_menu_removes_every_dish(drain, sessions, clock):
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()

    await _stage(sessions, payload=_payload(menu={"categories": []}), now=clock.now)
    clock.advance(31)
    await drain.tick()
    assert await _rows(sessions, item_chunks) == []
    assert len(await _rows(sessions, restaurant_chunks)) == 1  # the restaurant remains


async def test_one_restaurants_reconcile_does_not_touch_another(drain, sessions, clock):
    await _stage(sessions, "r1")
    await _stage(sessions, "r2")
    clock.advance(31)
    await drain.tick()

    await _stage(sessions, "r1", _payload(menu={"categories": []}), now=clock.now)
    clock.advance(31)
    await drain.tick()
    assert [r["restaurant_id"] for r in await _rows(sessions, item_chunks)] == ["r2"]


# ── the gap between reading and writing ─────────────────────────────


async def test_an_edit_arriving_mid_drain_is_not_lost(sessions, embeddings, clock):
    """The race the guarded completion exists for. The drain reads a row,
    then spends seconds embedding with no transaction open; an unguarded
    delete would discard whatever landed in that gap, and nothing anywhere
    would report the index as stale."""

    class RacingEmbeddings(CountingEmbeddings):
        async def embed(self, texts):
            # A new event lands while the provider call is in flight.
            await _stage(
                sessions, payload=_payload(menu=_menu(_item(name="Renamed"))), now=clock.now
            )
            return await super().embed(texts)

    await _stage(sessions)
    clock.advance(31)
    await KnowledgeDrain(sessions, RacingEmbeddings(), interval_s=0.0, batch=20, clock=clock).tick()

    still_queued = await _rows(sessions, knowledge_pending)
    assert [r["restaurant_id"] for r in still_queued] == ["r1"]


async def test_a_redelivery_of_the_same_payload_still_completes(drain, sessions, clock):
    """The other half: an identical payload compares equal, so it is
    correctly treated as work already done rather than re-queued forever."""
    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    assert await _rows(sessions, knowledge_pending) == []


# ── ordering and batching ───────────────────────────────────────────


async def test_the_stalest_restaurant_drains_first(drain, sessions, clock):
    await _stage(sessions, "old", now=T0)
    await _stage(sessions, "new", now=T0 + timedelta(seconds=10))
    clock.advance(60)
    async with sessions() as session:
        due = await PendingRepo(session).due(now=clock.now, limit=20)
    assert [p.restaurant_id for p in due] == ["old", "new"]


async def test_the_batch_size_caps_one_pass(sessions, embeddings, clock):
    for index in range(5):
        await _stage(sessions, f"r{index}")
    clock.advance(31)
    small = KnowledgeDrain(sessions, embeddings, interval_s=0.0, batch=2, clock=clock)
    assert await small.tick() == 2
    assert await small.tick() == 2
    assert await small.tick() == 1
    assert await small.tick() == 0


# ── rows the consumer would never have staged ───────────────────────


async def test_a_hand_staged_unindexable_row_is_cleared_not_retried(drain, sessions, clock):
    """Unreachable through the consumer, which applies the same predicate
    before queueing — but reachable from an older build or an operator's
    hand, and a row that can never be indexed must not sit in the queue
    forever pretending to be a backlog."""
    payload = _payload(kind="brand", city=None)
    async with sessions() as session:
        await PendingRepo(session).stage(
            restaurant_id="b1",
            payload=payload,
            now=T0,
            debounce_s=30.0,
        )
        await session.commit()
    clock.advance(31)
    assert await drain.tick() == 0
    assert await _rows(sessions, knowledge_pending) == []
    assert await _rows(sessions, item_chunks) == []


async def test_a_text_with_no_words_still_gets_a_usable_vector(drain, sessions, clock):
    """A zero vector has no direction and `<=>` against it is undefined in
    pgvector, so a text with nothing to hash is parked on a fixed unit
    vector rather than left at the origin.

    Reachable through a RESTAURANT chunk, whose text is just the display
    name and cuisines. An item chunk always carries the literal "Category:"
    label, so it can never be token-free — which is worth knowing before
    someone "simplifies" that label away."""
    await _stage(sessions, payload=_payload(name="...", branch_label=None, cuisines=[]))
    clock.advance(31)
    await drain.tick()
    vector = list((await _rows(sessions, restaurant_chunks))[0]["embedding"])
    assert vector[0] == 1.0 and sum(abs(v) for v in vector) == 1.0


async def test_the_loop_survives_a_failing_pass(drain, monkeypatch):
    """Supervised forever, like the outbox poller: a failed pass logs and
    retries rather than killing the task. Nothing was completed, so the row
    is still queued and the next tick picks it up — which is why the failure
    counter is a retry rate and not a loss rate.

    (This used to be covered only by accident, when the unit suite still
    started the real consumer wiring against whatever broker happened to be
    running. Making the suite hermetic left it untested.)"""
    import asyncio

    passes = {"n": 0}

    async def flaky() -> int:
        passes["n"] += 1
        if passes["n"] == 1:
            raise RuntimeError("provider down")
        raise asyncio.CancelledError

    monkeypatch.setattr(drain, "tick", flaky)
    monkeypatch.setattr(drain, "_interval_s", 0)
    with pytest.raises(asyncio.CancelledError):
        await drain.run()
    assert passes["n"] == 2  # the failure did not end the loop


def test_the_default_clock_is_the_wall_clock():
    """The drain takes an injected clock everywhere else, so this default is
    the one line of it the rest of the suite never reaches — and it is the
    one production actually runs."""
    from datetime import UTC, datetime

    from ai_assistant.drain import _utc_now

    assert (datetime.now(UTC) - _utc_now()).total_seconds() < 5


# ── the staleness signal NFR-28 actually pages on ───────────────────


async def test_the_backlog_gauge_rises_when_nothing_drains(drain, sessions, clock):
    """The freshness HISTOGRAM is observed only on a successful index, so a
    drain that never succeeds observes nothing at all — every bucket's rate
    is zero, `histogram_quantile` returns NaN, and the NFR-28 page stays
    inactive in precisely the outage it exists for. A gauge over the backlog
    has the opposite failure mode: it rises when nothing is draining.

    Here the embedding provider is down, so every pass raises and the queue
    never clears — and the number an operator pages on keeps climbing.
    """
    from ai_assistant.domain.ports import EmbeddingUnavailable
    from ai_assistant.metrics import KNOWLEDGE_BACKLOG_SECONDS

    class Down:
        model = "down"
        dimensions = DIMENSIONS

        async def embed(self, texts):
            raise EmbeddingUnavailable("provider down")

    stalled = KnowledgeDrain(sessions, Down(), interval_s=0.0, batch=20, clock=clock)
    await _stage(sessions)
    clock.advance(31)

    with pytest.raises(EmbeddingUnavailable):
        await stalled.tick()
    first = KNOWLEDGE_BACKLOG_SECONDS._value.get()
    assert first >= 31  # it has been waiting since it was staged

    clock.advance(600)
    with pytest.raises(EmbeddingUnavailable):
        await stalled.tick()
    assert KNOWLEDGE_BACKLOG_SECONDS._value.get() > first  # and it keeps climbing
    assert await _rows(sessions, knowledge_pending) != []  # still undrained


async def test_an_empty_queue_reads_zero_not_the_last_value(drain, sessions, clock):
    """A stale carry-over would page forever after one slow change."""
    from ai_assistant.metrics import KNOWLEDGE_BACKLOG_SECONDS

    await _stage(sessions)
    clock.advance(31)
    await drain.tick()
    await drain.tick()  # nothing left to do
    assert KNOWLEDGE_BACKLOG_SECONDS._value.get() == 0.0
