"""`Retriever` over assistant_db — four queries, two fusions (FR-62).

Pure-Postgres SQL, so like catalog's `search.py` its `execute` calls run
against a stub session in the unit suite and its matching quality is proven
live. What IS unit-tested here is everything around the SQL: that the active
version is read rather than assumed, that both session-level settings are
applied, that the per-leg fetch is wider than the returned limit, and that
an empty index does not become an empty-string query against the provider.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..domain.ports import EmbeddingPort, Retrieved
from ..domain.retrieval import (
    SET_THRESHOLD,
    Candidate,
    Filters,
    Passage,
    fuse,
    lexical_sql,
    vector_sql,
)
from .vector_store import PostgresVectorStore

LEG_OVERFETCH = 4
"""Each leg fetches `limit * LEG_OVERFETCH` rows before fusion.

Fusion needs more candidates than it returns or it cannot do its job: a
result that both legs rank middling is exactly what RRF promotes, and it
only exists in the merged set if both legs were asked deep enough to include
it. Fetching exactly `limit` per leg would turn the fusion into "whichever
leg happened to rank it first".
"""


class PostgresRetriever:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        embeddings: EmbeddingPort,
        *,
        ef_search: int | None = None,
    ) -> None:
        self._sessions = sessions
        self._embeddings = embeddings
        self._ef_search = ef_search

    async def retrieve(self, *, query: str, filters: Filters, limit: int) -> Retrieved:
        """A sessionmaker rather than a session, matching catalog's
        `PostgresSearch`: the object is built once at startup and owns a
        session per call, so the API layer never has to touch an adapter to
        open one (the layer contract forbids exactly that)."""
        async with self._sessions() as session:
            return await self._retrieve(session, query, filters, limit)

    async def _retrieve(
        self, session: AsyncSession, query: str, filters: Filters, limit: int
    ) -> Retrieved:
        await self._apply_settings(session)
        (vector,) = await self._embeddings.embed([query])
        fetch = limit * LEG_OVERFETCH
        common = {"q": query, "leg_limit": fetch}
        # pgvector binds a vector from its text form; going through the
        # driver's list adaptation would need the dialect's type on a raw
        # textual query, which is exactly what this SQL avoids.
        common["query_vector"] = "[" + ",".join(repr(float(v)) for v in vector) + "]"

        items = await self._leg_pair(session, filters, items=True, common=common, limit=limit)
        restaurants = await self._leg_pair(
            session, filters, items=False, common=common, limit=limit
        )
        return Retrieved(items=items, restaurants=restaurants, query_vector=vector)

    async def texts_in_order(self, candidates: Sequence[Candidate]) -> list[Passage]:
        """Ranked candidates, hydrated, RANK PRESERVED.

        The dict a bulk read returns is keyed by id and has no order; handing
        the model candidates in database order instead of relevance order
        would waste the ranking the fusion just computed.
        """
        if not candidates:
            return []
        async with self._sessions() as session:
            found = await PostgresVectorStore(session).texts_for(
                chunk_ids=[c.chunk_id for c in candidates]
            )
        return [
            Passage(
                item_id=found[c.chunk_id][0],
                restaurant_id=c.restaurant_id,
                text=found[c.chunk_id][1],
            )
            for c in candidates
            if c.chunk_id in found
        ]

    async def _apply_settings(self, session: AsyncSession) -> None:
        """Both knobs are set per-connection IN CODE rather than trusted to a
        default, for the same reason ADR-0019 gave: a fresh environment must
        not silently run with different matching behaviour from the one the
        thresholds were measured against."""
        await session.execute(sa.text(SET_THRESHOLD))
        if self._ef_search is not None:
            # An OVERRIDE, not the mechanism. `hnsw.*` GUCs are registered
            # by pgvector's library, which a backend loads on first use of
            # the type, so a bare SET on a fresh connection can land as an
            # unrecognised placeholder and do nothing at all. What actually
            # guarantees correct filtered recall is the database-level
            # `hnsw.iterative_scan` set as superuser in initdb; this knob is
            # for tuning a specific deployment on top of it.
            await session.execute(sa.text(f"SET hnsw.ef_search = {int(self._ef_search)}"))

    async def _leg_pair(
        self,
        session: AsyncSession,
        filters: Filters,
        *,
        items: bool,
        common: dict[str, object],
        limit: int,
    ) -> Sequence[Candidate]:
        lexical_query, params = lexical_sql(filters, items=items)
        vector_query, _ = vector_sql(filters, items=items)
        bound = {**params, **common}
        lexical = await self._run(session, lexical_query, bound)
        vector = await self._run(session, vector_query, bound)
        return fuse(lexical, vector, limit=limit)

    async def _run(
        self, session: AsyncSession, sql: str, params: dict[str, object]
    ) -> list[Candidate]:
        rows = await session.execute(sa.text(sql), params)
        return [
            Candidate(
                chunk_id=row.id,
                restaurant_id=row.restaurant_id,
                item_id=row.item_id,
                score=0.0,  # rank is the currency; the raw score is not used
            )
            for row in rows
        ]
