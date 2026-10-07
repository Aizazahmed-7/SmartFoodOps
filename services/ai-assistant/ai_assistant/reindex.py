"""Rolling reindex: move the corpus to a new vector space (FR-61).

A model or dimension change makes every stored vector incomparable with
every new one — they are different spaces, not different versions of one
space. The naive response is to truncate and rebuild, which means minutes
with an empty index and a search box that answers nothing. This does it the
other way round:

    1. write the new generation BESIDE the old one, batch by batch
       (the composite PK `(id, model_version)` is what allows both)
    2. flip `knowledge_index_state` once every chunk exists
    3. delete the old generation, which no query can be reading by then

Queries filter on the active version throughout, so the two generations are
never mixed in one result set — they are not even visible to the same query.

Nothing here needs catalog or Kafka: the text that was embedded is stored on
the row, so a reindex is a read of our own table, an embed, and a write.

**Resumable by construction.** The batch query is an anti-join — rows under
the active version with no counterpart under the target — so a killed
worker's replacement simply finds fewer rows. There is no cursor to lose and
no half-finished state to clean up, which is what makes running under
Celery's at-least-once delivery safe.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import IndexStateRepo
from .adapters.vector_store import PostgresVectorStore
from .domain.ports import EmbeddingPort
from .drain import model_version
from .metrics import EMBED_REQUESTS, KNOWLEDGE_CHUNKS

log = get_logger("ai-assistant.reindex")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ReindexResult:
    """What one invocation achieved. `done` is the operator's signal to stop
    re-running it — a reindex is bounded work, not a daemon."""

    active_version: str
    target_version: str
    migrated: int
    done: bool
    activated: bool = False
    retired: int = 0


async def run_reindex(
    sessions: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingPort,
    *,
    batch: int = 200,
    max_batches: int = 50,
    clock: Callable[[], datetime] = _utc_now,
) -> ReindexResult:
    """Migrate up to `batch * max_batches` chunks, then report.

    Bounded rather than looping to completion so one invocation cannot hold
    a worker for an hour on a large corpus. The caller re-runs until
    `done` — and because the work is resumable, re-running is the only
    recovery procedure there is.
    """
    target = model_version(embeddings)
    async with sessions() as session:
        active = await IndexStateRepo(session).active()

    if active is None or active == target:
        # Nothing to move: either no index has been built yet, or the
        # configured space is already the live one. Both are the normal
        # state — a reindex is a no-op unless someone changed the model.
        return ReindexResult(
            active_version=active or target, target_version=target, migrated=0, done=True
        )

    migrated = 0
    for _ in range(max_batches):
        moved = await _one_batch(sessions, embeddings, active, target, batch, clock)
        migrated += moved
        if moved == 0:
            break
    else:
        log.info(
            "reindex batch limit reached — re-run to continue",
            active_version=active,
            target_version=target,
            migrated=migrated,
        )
        return ReindexResult(
            active_version=active, target_version=target, migrated=migrated, done=False
        )

    now = clock()
    async with sessions() as session:
        await IndexStateRepo(session).activate(model_version=target, now=now)
        # Only after the pointer moves: until this commit, a reader could
        # still be mid-query against the old generation.
        retired = await PostgresVectorStore(session).drop_version(model_version=active)
        await session.commit()
    log.info(
        "reindex complete — index activated",
        previous_version=active,
        active_version=target,
        migrated=migrated,
        retired=retired,
    )
    return ReindexResult(
        active_version=target,
        target_version=target,
        migrated=migrated,
        done=True,
        activated=True,
        retired=retired,
    )


async def _one_batch(
    sessions: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingPort,
    active: str,
    target: str,
    batch: int,
    clock: Callable[[], datetime],
) -> int:
    async with sessions() as session:
        store = PostgresVectorStore(session)
        pending = await store.awaiting_migration(
            active_version=active, target_version=target, limit=batch
        )
        if not pending:
            return 0
        # Borrow anything this reindex already wrote: the fan-out means a
        # base dish shared by twelve branches is twelve rows of identical
        # text, and an earlier batch may already have paid for it.
        borrowed = await store.vectors_by_hash(
            content_hashes=[row["content_hash"] for _, row in pending],
            model_version=target,
        )

    texts = {
        row["content_hash"]: row["content"]
        for _, row in pending
        if row["content_hash"] not in borrowed
    }
    vectors = dict(borrowed)
    if texts:
        computed = await embeddings.embed(list(texts.values()))
        vectors.update(dict(zip(texts.keys(), computed, strict=True)))
        EMBED_REQUESTS.labels(outcome="sent").inc()

    now = clock()
    async with sessions() as session:
        store = PostgresVectorStore(session)
        for table, row in pending:
            await store.copy_forward(
                table=table,
                row=row,
                target_version=target,
                embedding=vectors[row["content_hash"]],
                now=now,
            )
            KNOWLEDGE_CHUNKS.labels(
                kind="item" if table.name == "item_chunks" else "restaurant",
                result="reindexed",
            ).inc()
        await session.commit()
    return len(pending)


def main() -> None:  # pragma: no cover — the operator entry point
    """`make reindex`. The same work the Celery task does, run in the
    foreground against the configured database.

    Dev compose runs no assistant worker — the AI plane is already ~5 GB on
    a 7.7 GB box — and a reindex is a supervised, occasional operation, so
    running it in the foreground is the honest local shape. In a deployment
    it rides the `assistant.reindex` queue like any other batch job.
    """
    import asyncio

    from .config import Settings
    from .tasks import _run

    print(asyncio.run(_run(Settings())))


if __name__ == "__main__":  # pragma: no cover
    main()
