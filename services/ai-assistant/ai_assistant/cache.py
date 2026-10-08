"""The answer cache, as the graph sees it (FR-74, ADR-0045).

Lives here rather than in `domain/` because it resolves the fence from the
database, and rather than in `api/` because the layer contract forbids
routes touching adapters. What it adds over the two adapters is the thing
neither of them can know alone: what fence applies right now.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import EpochRepo
from .domain.answers import Cached, Fence

log = get_logger("ai-assistant.cache")


class AnswerCache:
    """Both tiers behind one object, and one place that builds the fence.

    The fence is resolved per lookup rather than captured at startup,
    because both of its moving parts move while the process is up: the
    active model version changes under a rolling reindex (FR-61), and the
    epoch changes every time the drain touches the city. A fence captured
    at boot would keep serving answers from a corpus that had already
    changed, which is precisely the failure FR-74 asks us to prevent.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        exact_tier: Any,
        semantic_tier: Any,
    ) -> None:
        self._sessions = sessions
        self._exact = exact_tier
        self._semantic = semantic_tier

    async def _fence(self, city: str) -> Fence:
        """Always resolvable now that the model version is configuration
        rather than a row: the only variable left is the city's epoch, and
        a city with no bumps yet is epoch 0, not an absence."""
        async with self._sessions() as session:
            epoch = await EpochRepo(session).current(city)
        return Fence(city=city, epoch=epoch)

    async def exact(self, question: str, city: str) -> tuple[Cached | None, Fence]:
        """The hit, AND the fence it was looked up under.

        The fence travels out so the write-back can use the one that was
        current when the corpus was READ. Resolving it again at write time
        was a real staleness bug: a generation takes seconds, the drain
        ticks every five, and an answer computed at epoch N was being stored
        under epoch N+1 — the one epoch it is definitely wrong for. It then
        served every later asker, which is precisely the window ADR-0045 §3
        claims does not exist.
        """
        fence = await self._fence(city)
        return await self._exact.get(fence=fence, question=question), fence

    async def semantic(self, query_vector: Sequence[float], fence: Fence) -> Cached | None:
        """Takes the fence rather than resolving one: by now retrieval has
        happened, and the answer belongs to the corpus that was read."""
        return await self._semantic.get(fence=fence, query_vector=query_vector)

    async def remember(
        self, fence: Fence, question: str, query_vector: Sequence[float], cached: Cached
    ) -> None:
        """Write both tiers, and never let either break the turn.

        The answer is already correct and already streaming by the time this
        runs; a cache write that raised here would turn a good turn into a
        failed one to save a future turn some latency. Swallowed and logged,
        which also keeps a cold Redis from being an outage.

        A write under a SUPERSEDED epoch is harmless — nothing will ever
        read it. A write under a future one is the bug above.
        """
        try:
            await self._exact.put(fence=fence, question=question, cached=cached)
            # Only the exact tier can serve a turn that never retrieved, so
            # a missing vector is a write the semantic tier cannot make.
            if query_vector:
                await self._semantic.put(
                    fence=fence,
                    question=question,
                    query_vector=query_vector,
                    cached=cached,
                    now=datetime.now(UTC),
                )
        except Exception as exc:
            log.warning("answer not cached", error=str(exc))
