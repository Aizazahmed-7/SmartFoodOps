"""The knowledge drain: pending queue -> embeddings -> index (FR-58).

Runs in the service process rather than on Celery, and the split is
ADR-0025's: this is a PROJECTION in the freshness hot path, and it must run
whenever the service runs. Celery gets the reindex (FR-61) — rare, long,
and wanting its own queue and retry schedule.

The shape of one pass is dictated by one constraint: **the provider call
happens outside any transaction.** Holding a transaction open across seconds
of network is how a connection pool dies. So a pass reads, then embeds, then
writes — and because the world can move in the gap, the write completes with
a guarded delete (`PendingRepo.complete`) rather than an unconditional one.

Three things stop the provider being called:
- the chunk's text already matches the stored row (nothing changed)
- the same text already has a vector somewhere in the index (the fan-out)
- the drain runs at all: a menu with no textual change costs zero calls
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import EpochRepo, Pending, PendingRepo
from .adapters.vector_store import PostgresVectorStore
from .domain.knowledge import RestaurantKnowledge, chunk
from .domain.ports import EmbeddingPort, ItemUpsert, RestaurantUpsert
from .metrics import (
    DRAIN_FAILURES,
    EMBED_REQUESTS,
    KNOWLEDGE_BACKLOG_SECONDS,
    KNOWLEDGE_CHUNKS,
    KNOWLEDGE_FRESHNESS_SECONDS,
)

log = get_logger("ai-assistant.drain")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _backlog_seconds(pending: "list[Pending]", now: datetime) -> float:
    """How long the oldest waiting menu change has been waiting."""
    if not pending:
        return 0.0
    oldest = min(row.first_seen_at for row in pending)
    if oldest.tzinfo is None:  # sqlite hands back naive; Postgres does not
        oldest = oldest.replace(tzinfo=UTC)
    return max((now - oldest).total_seconds(), 0.0)


class KnowledgeDrain:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embeddings: EmbeddingPort,
        *,
        interval_s: float,
        batch: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._sessions = sessions
        self._embeddings = embeddings
        self._interval_s = interval_s
        self._batch = batch
        self._clock = clock

    async def run(self) -> None:
        """Supervised forever, like the outbox poller and the consumer loop:
        a failed pass logs and retries rather than killing the task. Nothing
        was completed, so the row is still queued and the next tick picks it
        up — which is why the failure counter is a retry rate, not a loss
        rate."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover — supervision, tested via tick()
                DRAIN_FAILURES.inc()
                log.error("drain pass failed — retrying", error=str(exc))
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> int:
        """One pass. Returns how many restaurants were indexed."""
        now = self._clock()
        async with self._sessions() as session:
            pending = await PendingRepo(session).due(now=now, limit=self._batch)
        # Set BEFORE draining, so a pass that fails on every row still
        # reports the backlog it could not clear. Zero on an empty queue,
        # never a stale carry-over.
        KNOWLEDGE_BACKLOG_SECONDS.set(_backlog_seconds(pending, now))
        drained = 0
        for row in pending:
            # Sequentially: each restaurant commits before the next is read,
            # so one slow provider call cannot fan out into N concurrent
            # ones against a rate-limited endpoint.
            drained += await self._drain_one(row)
        return drained

    async def _drain_one(self, pending: Pending) -> int:
        knowledge = chunk(restaurant_id=pending.restaurant_id, payload=pending.payload)
        if knowledge is None:
            # Unreachable through the consumer, which applies the same
            # predicate before queueing — but a row can also arrive from an
            # older build or an operator's hand. Completing it is right:
            # there is nothing to index and nothing to retry.
            await self._complete(pending)
            return 0

        async with self._sessions() as session:
            store = PostgresVectorStore(session)
            stored = await store.contents_for(restaurant_id=pending.restaurant_id)
            # Read before the rewrite: what retrieval can currently see, and
            # where. Both are compared against the new rows below to decide
            # whether the answer cache's fence has to move.
            was_retrievable = await store.retrieval_state(restaurant_id=pending.restaurant_id)
            was_in = await store.cities_for(restaurant_id=pending.restaurant_id)
            stale = self._stale(knowledge, stored)

        # One batched call for everything whose text changed. `_stale` is
        # keyed by content_hash, so a dish listed twice in this restaurant
        # is still embedded once.
        vectors: dict[str, list[float]] = {}
        if stale:
            computed = await self._embeddings.embed([text for _, text in stale])
            vectors = dict(zip((h for h, _ in stale), computed, strict=True))

        now = self._clock()
        async with self._sessions() as session:
            deleted = await PostgresVectorStore(session).replace_restaurant(
                restaurant_id=pending.restaurant_id,
                restaurant=RestaurantUpsert(
                    chunk=knowledge.restaurant,
                    embedding=vectors.get(knowledge.restaurant.content_hash),
                ),
                items=[
                    ItemUpsert(chunk=item, embedding=vectors.get(item.content_hash))
                    for item in knowledge.items
                ],
                now=now,
            )
            kept = await PendingRepo(session).complete(
                restaurant_id=pending.restaurant_id, payload_hash=pending.payload_hash
            )
            # The answer cache's fence, moved in the same transaction as the
            # chunks it fences (FR-74). Only when something ACTUALLY changed:
            # a pass that re-confirms an unchanged menu would otherwise cold-
            # start the whole city's cache on every catalog heartbeat.
            #
            # "Changed" means anything RETRIEVAL can see, not just text: an
            # 86'd dish and a paused kitchen both leave every content hash
            # identical while changing what a query returns.
            now_retrievable = await PostgresVectorStore(session).retrieval_state(
                restaurant_id=pending.restaurant_id
            )
            if stale or deleted or now_retrievable != was_retrievable:
                # Every city the restaurant was in AND is now in. A branch
                # that moves leaves the city it left holding cached answers
                # that recommend it.
                for city in was_in | {knowledge.restaurant.city}:
                    await EpochRepo(session).bump(city=city, now=now)
            await session.commit()

        self._record(knowledge, stored, len(stale), deleted, pending, now)
        if not kept:
            log.info(
                "restaurant changed again mid-drain — re-queued",
                restaurant_id=pending.restaurant_id,
            )
        return 1

    def _stale(
        self, knowledge: RestaurantKnowledge, stored: dict[str, str]
    ) -> list[tuple[str, str]]:
        """`(content_hash, text)` for every chunk whose text differs from
        what is stored under its id.

        Compares the TEXT itself rather than a stored digest: `content` is
        kept on the row for B2's lexical leg anyway, so a second derived
        column would only be a cheaper way to ask the same question.

        De-duplicated WITHIN the restaurant too: a menu that lists the same
        drink under two categories is two chunks and one vector.
        """
        stale: dict[str, str] = {}

        def consider(chunk_id: str, content_hash: str, content: str) -> None:
            if stored.get(chunk_id) != content:
                # Keyed by the digest so two chunks sharing text share one
                # embedding — the digest is derived in `chunk()` and never
                # stored, it only has to be stable within this pass.
                stale.setdefault(content_hash, content)

        restaurant = knowledge.restaurant
        consider(restaurant.id, restaurant.content_hash, restaurant.content)
        for item in knowledge.items:
            consider(item.id, item.content_hash, item.content)
        return list(stale.items())

    async def _complete(self, pending: Pending) -> None:
        async with self._sessions() as session:
            await PendingRepo(session).complete(
                restaurant_id=pending.restaurant_id, payload_hash=pending.payload_hash
            )
            await session.commit()

    def _record(
        self,
        knowledge: RestaurantKnowledge,
        stored: dict[str, str],
        embedded: int,
        deleted: int,
        pending: Pending,
        now: datetime,
    ) -> None:
        EMBED_REQUESTS.labels(outcome="sent" if embedded else "skipped").inc()

        def count(kind: str, chunk_id: str, content: str) -> None:
            result = "unchanged" if stored.get(chunk_id) == content else "embedded"
            KNOWLEDGE_CHUNKS.labels(kind=kind, result=result).inc()

        count("restaurant", knowledge.restaurant.id, knowledge.restaurant.content)
        for item in knowledge.items:
            count("item", item.id, item.content)
        if deleted:
            KNOWLEDGE_CHUNKS.labels(kind="item", result="deleted").inc(deleted)
        first_seen = pending.first_seen_at
        if first_seen.tzinfo is None:  # sqlite hands back naive; Postgres does not
            first_seen = first_seen.replace(tzinfo=UTC)
        KNOWLEDGE_FRESHNESS_SECONDS.observe((now - first_seen).total_seconds())
        log.info(
            "restaurant indexed",
            restaurant_id=pending.restaurant_id,
            items=len(knowledge.items),
            embedded=embedded,
            deleted=deleted,
        )
