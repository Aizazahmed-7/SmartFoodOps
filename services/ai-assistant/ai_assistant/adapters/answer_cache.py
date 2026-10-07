"""The two cache tiers (FR-74, ADR-0045).

Exact match lives in Redis and skips the entire turn. Semantic match lives
in Postgres beside the vectors it compares against, and skips the
generation. Both are fenced identically and both fail OPEN: a cache that
can break a turn is a liability, so every error here degrades to a miss and
the turn proceeds as if the cache were cold.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import answer_cache
from ..domain.answers import EXACT, SEMANTIC, Cached, Fence
from ..metrics import CACHE

log = get_logger("ai-assistant.cache")


def _miss(tier: str) -> None:
    CACHE.labels(tier=tier, result="miss").inc()


def _hit(tier: str) -> None:
    CACHE.labels(tier=tier, result="hit").inc()


class RedisExactCache:
    """Tier 1: the same question, asked again.

    A string GET against a key that already contains the fence, which is
    why this can run before retrieval and before any embedding — the whole
    turn collapses to one round trip.
    """

    def __init__(self, redis: Any, *, ttl_s: int) -> None:
        self._r = redis
        self._ttl = ttl_s

    async def get(self, *, fence: Fence, question: str) -> Cached | None:
        """A miss on ANY failure, including a body we cannot parse.

        The `try` used to cover only the Redis call, and the hit was counted
        before the parse — so an entry written by an older build (the fence
        has no body-schema component, so keys survive a deploy) raised a
        `KeyError`, counted as a hit, and failed the customer's turn. Found
        by the B3 review; the class promised "every error degrades to a
        miss" and two lines of it did not.
        """
        try:
            raw = await self._r.get(fence.key(question))
            if not raw:
                _miss(EXACT)
                return None
            body = json.loads(raw)
            cached = Cached(
                answer=body["answer"],
                item_ids=body.get("item_ids", []),
                restaurant_ids=body.get("restaurant_ids", []),
            )
        except Exception as exc:
            log.warning("exact cache read failed — treating as a miss", error=str(exc))
            _miss(EXACT)
            return None
        _hit(EXACT)
        return cached

    async def put(self, *, fence: Fence, question: str, cached: Cached) -> None:
        """A TTL as well as the fence, and they cover different things.

        The fence catches menu changes. The TTL catches everything else a
        cached answer can go stale against and nothing tells us about — a
        prompt edit, a model swap, a grounding rule tightened — so it is the
        bound on how long a change we forgot to fence takes to disappear.
        """
        body = json.dumps(
            {
                "answer": cached.answer,
                "item_ids": list(cached.item_ids),
                "restaurant_ids": list(cached.restaurant_ids),
            }
        )
        try:
            await self._r.set(fence.key(question), body, ex=self._ttl)
        except Exception as exc:  # pragma: no cover — live Redis failure
            log.warning("exact cache write failed — answer not cached", error=str(exc))


class PostgresSemanticCache:
    """Tier 2: somebody asked something close enough, recently enough.

    Runs AFTER retrieval on purpose. The query vector is retrieval's own
    output, so asking here costs one indexed ANN lookup and no provider
    call; running it before retrieval would mean embedding every question
    twice, and a cache that doubles the embedding bill on every miss is not
    a cache.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        threshold: float,
        ttl_s: int = 3600,
    ) -> None:
        self._sessions = sessions
        self._threshold = threshold
        self._ttl = ttl_s

    async def get(self, *, fence: Fence, query_vector: Sequence[float]) -> Cached | None:
        try:
            return await self._get(fence=fence, query_vector=query_vector)
        except Exception as exc:
            # A miss, not a failed turn. Retrieval has already succeeded by
            # the time this runs, so a pool timeout here was turning a
            # question that would have generated fine into "Sorry, I
            # couldn't finish that" (B3 review).
            log.warning("semantic cache read failed — treating as a miss", error=str(exc))
            _miss(SEMANTIC)
            return None

    async def _get(self, *, fence: Fence, query_vector: Sequence[float]) -> Cached | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    sa.select(
                        answer_cache.c.answer,
                        answer_cache.c.item_ids,
                        answer_cache.c.restaurant_ids,
                        answer_cache.c.embedding.cosine_distance(list(query_vector)).label("d"),
                    )
                    .where(
                        answer_cache.c.model_version == fence.model_version,
                        answer_cache.c.city == fence.city,
                        answer_cache.c.epoch == fence.epoch,
                        # The same age bound the exact tier gets from its
                        # TTL. `created_at` was written and never read, so
                        # in a city whose menus are quiet nothing ever aged
                        # out — a prompt fix would ship and this tier would
                        # keep serving the pre-fix prose indefinitely
                        # (B3 review). The fence covers corpus changes; this
                        # covers every change the fence cannot see.
                        answer_cache.c.created_at
                        >= datetime.now(UTC) - timedelta(seconds=self._ttl),
                    )
                    .order_by(sa.text("d"))
                    .limit(1)
                )
            ).first()
        # The threshold is checked HERE rather than in the WHERE clause: an
        # ANN index orders by distance and does not filter by it, so a
        # predicate would be applied after the scan anyway — and reading the
        # nearest row's actual distance is what makes the cut tunable
        # against real data instead of guessed at.
        if row is None or float(row.d) > self._threshold:
            _miss(SEMANTIC)
            return None
        _hit(SEMANTIC)
        return Cached(
            answer=row.answer,
            item_ids=list(row.item_ids),
            restaurant_ids=list(row.restaurant_ids),
        )

    async def put(
        self,
        *,
        fence: Fence,
        question: str,
        query_vector: Sequence[float],
        cached: Cached,
        now: datetime,
    ) -> None:
        """Keyed by the normalized question and the model version, so asking
        the same thing twice overwrites rather than accumulates. The city
        and epoch ride as columns: a re-ask after a menu change replaces the
        row and the old fence simply stops matching."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        dialect = "postgresql"
        async with self._sessions() as session:
            bind = session.bind
            dialect = bind.dialect.name if bind is not None else "sqlite"
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            values = {
                "id": fence.row_id(question),
                "model_version": fence.model_version,
                "city": fence.city,
                "epoch": fence.epoch,
                "question": question,
                "embedding": list(query_vector),
                "answer": cached.answer,
                "item_ids": list(cached.item_ids),
                "restaurant_ids": list(cached.restaurant_ids),
                "created_at": now,
            }
            stmt = insert(answer_cache).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[
                        answer_cache.c.id,
                        answer_cache.c.model_version,
                        answer_cache.c.city,
                    ],
                    set_={
                        k: v for k, v in values.items() if k not in ("id", "model_version", "city")
                    },
                )
            )
            await session.commit()
