"""The rolling reindex (FR-61), against real sqlite.

The property under test is not "the new vectors appear" — it is that the
two generations are never mixed and never absent. A reindex that truncated
and rebuilt would pass an "index has new vectors" assertion and leave the
search box answering nothing for the duration.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.embeddings_fake import FakeEmbeddings
from ai_assistant.adapters.repo import IndexStateRepo
from ai_assistant.consumers import KnowledgeHandler
from ai_assistant.db import item_chunks, metadata, restaurant_chunks
from ai_assistant.drain import KnowledgeDrain, model_version
from ai_assistant.reindex import run_reindex
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _payload(item_ids=("i1", "i2")):
    return {
        "kind": "branch",
        "name": "Biryani House",
        "branch_label": "Downtown",
        "city": "springfield",
        "cuisines": ["pakistani"],
        "status": "open",
        "menu": {
            "categories": [
                {
                    "name": "Mains",
                    "items": [
                        {
                            "id": item_id,
                            "name": f"Dish {item_id}",
                            "price_cents": 500,
                            "available": True,
                            "tags": ["spicy"],
                        }
                        for item_id in item_ids
                    ],
                }
            ]
        },
    }


class Counting(FakeEmbeddings):
    def __init__(self, dimensions=8, model="fake-hashing-v1"):
        super().__init__(dimensions=dimensions, model=model)
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        return await super().embed(texts)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _build_index(sessions, embeddings, restaurant_ids=("r1",)):
    """A populated index under the CURRENT model, through the real pipeline."""
    for restaurant_id in restaurant_ids:
        await KnowledgeHandler(sessions, debounce_s=30.0, clock=lambda: T0).handle(
            {
                "aggregate_type": "restaurant",
                "aggregate_id": restaurant_id,
                "event_type": "ItemAdded",
                "payload": json.dumps(_payload()),
            }
        )
    await KnowledgeDrain(
        sessions, embeddings, interval_s=0.0, batch=20, clock=lambda: T0 + timedelta(seconds=31)
    ).tick()
    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version=model_version(embeddings), now=T0)
        await session.commit()


async def _versions(sessions, table):
    async with sessions() as session:
        rows = await session.execute(sa.select(table.c.model_version, table.c.id))
        return sorted((r.model_version, r.id) for r in rows)


async def _active(sessions):
    async with sessions() as session:
        return await IndexStateRepo(session).active()


# ── the no-op cases, which are the normal ones ──────────────────────


async def test_a_matching_configuration_is_a_no_op(sessions):
    old = Counting()
    await _build_index(sessions, old)
    result = await run_reindex(sessions, old, clock=lambda: T0)
    assert result.done and result.migrated == 0 and not result.activated


async def test_an_empty_index_has_nothing_to_migrate(sessions):
    """No pointer yet means no corpus yet — the drain will write the
    configured version directly, and there is nothing to roll."""
    result = await run_reindex(sessions, Counting(model="new-model"), clock=lambda: T0)
    assert result.done and result.migrated == 0


# ── the cutover ─────────────────────────────────────────────────────


async def test_every_chunk_moves_to_the_new_space(sessions):
    await _build_index(sessions, Counting())
    new = Counting(model="new-model")

    result = await run_reindex(sessions, new, clock=lambda: T0)

    assert result.activated and result.done
    assert await _active(sessions) == "new-model:8"
    assert [v for v, _ in await _versions(sessions, item_chunks)] == ["new-model:8"] * 2
    assert [v for v, _ in await _versions(sessions, restaurant_chunks)] == ["new-model:8"]


async def test_the_old_generation_is_retired_only_after_the_flip(sessions):
    """Ordering is the whole safety property. Deleting first would leave a
    window where the pointer names a generation whose rows are gone."""
    await _build_index(sessions, Counting())
    result = await run_reindex(sessions, Counting(model="new-model"), clock=lambda: T0)
    assert result.retired == 3  # two dishes and the restaurant
    async with sessions() as session:
        leftovers = await session.scalar(
            sa.select(sa.func.count())
            .select_from(item_chunks)
            .where(item_chunks.c.model_version == "fake-hashing-v1:8")
        )
    assert leftovers == 0


async def test_the_pointer_does_not_move_while_work_remains(sessions):
    """A half-migrated corpus must keep answering from the old generation.
    With the cap set to one small batch, the reindex reports `done: false`
    and leaves the pointer alone."""
    await _build_index(sessions, Counting())
    result = await run_reindex(
        sessions, Counting(model="new-model"), batch=1, max_batches=1, clock=lambda: T0
    )
    assert not result.done and not result.activated
    assert await _active(sessions) == "fake-hashing-v1:8"


async def test_both_generations_coexist_mid_flight(sessions):
    """The composite PK (id, model_version) is what allows it — and what
    makes "never mixed in one result set" a predicate rather than a race."""
    await _build_index(sessions, Counting())
    await run_reindex(
        sessions, Counting(model="new-model"), batch=1, max_batches=1, clock=lambda: T0
    )
    versions = {v for v, _ in await _versions(sessions, item_chunks)} | {
        v for v, _ in await _versions(sessions, restaurant_chunks)
    }
    assert versions == {"fake-hashing-v1:8", "new-model:8"}


async def test_re_running_resumes_and_finishes(sessions):
    """The only recovery procedure there is: the batch query is an
    anti-join, so a killed worker's replacement simply finds fewer rows."""
    await _build_index(sessions, Counting())
    new = Counting(model="new-model")
    first = await run_reindex(sessions, new, batch=1, max_batches=1, clock=lambda: T0)
    assert not first.done

    total = first.migrated
    while not (
        result := await run_reindex(sessions, new, batch=1, max_batches=1, clock=lambda: T0)
    ).done:
        total += result.migrated
    total += result.migrated
    assert total == 3
    assert await _active(sessions) == "new-model:8"


async def test_a_completed_reindex_re_run_changes_nothing(sessions):
    await _build_index(sessions, Counting())
    new = Counting(model="new-model")
    await run_reindex(sessions, new, clock=lambda: T0)
    before = await _versions(sessions, item_chunks)
    again = await run_reindex(sessions, new, clock=lambda: T0)
    assert again.done and again.migrated == 0 and not again.activated
    assert await _versions(sessions, item_chunks) == before


# ── cost ────────────────────────────────────────────────────────────


async def test_text_comes_from_the_stored_rows_not_from_catalog(sessions):
    """A reindex touches neither catalog nor Kafka: the row already holds
    exactly the text that was embedded."""
    await _build_index(sessions, Counting())
    new = Counting(model="new-model")
    await run_reindex(sessions, new, clock=lambda: T0)
    async with sessions() as session:
        contents = (await session.execute(sa.select(item_chunks.c.content))).scalars().all()
    assert all(c.startswith("Dish ") for c in contents)


async def test_a_shared_dish_is_re_embedded_once_across_branches(sessions):
    """The fan-out again, on the reindex path: two branches, identical dish
    text, one call's worth of work for it."""
    await _build_index(sessions, Counting(), restaurant_ids=("r1", "r2"))
    new = Counting(model="new-model")
    await run_reindex(sessions, new, clock=lambda: T0)

    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    sa.select(item_chunks.c.content_hash, item_chunks.c.embedding).where(
                        item_chunks.c.content.like("Dish i1%")
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len({r["content_hash"] for r in rows}) == 1
    assert len({tuple(r["embedding"]) for r in rows}) == 1


# ── the pointer's own rule ──────────────────────────────────────────


async def test_the_pointer_is_adopted_once_and_never_overwritten(sessions):
    """`ensure` is insert-if-absent on purpose. An upsert would mean that
    changing the model and restarting silently repoints every query at a
    generation with no rows in it — an index that answers nothing and
    recovers quietly enough that nobody notices it was wrong."""
    async with sessions() as session:
        repo = IndexStateRepo(session)
        await repo.ensure(model_version="first:8", now=T0)
        await repo.ensure(model_version="second:8", now=T0)
        await session.commit()
    assert await _active(sessions) == "first:8"


async def test_only_activation_moves_the_pointer(sessions):
    async with sessions() as session:
        repo = IndexStateRepo(session)
        await repo.ensure(model_version="first:8", now=T0)
        await repo.activate(model_version="second:8", now=T0)
        await session.commit()
    assert await _active(sessions) == "second:8"


async def test_the_default_clock_is_utc_aware(sessions):
    """Every test above injects a clock, so the production default would
    otherwise go unexercised — and `updated_at` is a tz-aware column."""
    await _build_index(sessions, Counting())
    await run_reindex(sessions, Counting(model="new-model"))
    async with sessions() as session:
        stamped = await session.scalar(sa.select(item_chunks.c.updated_at).limit(1))
    assert abs((stamped.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()) < 60
