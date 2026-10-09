"""`PostgresRetriever` against a stub session.

The SQL is pure Postgres and cannot run on sqlite, so — exactly as catalog
does for its FTS adapter — matching quality is proven by the live smoke and
what is asserted here is everything AROUND the SQL: the version it reads,
the settings it applies, how deep each leg fetches, and the case where there
is no index at all.
"""

from types import SimpleNamespace
from typing import cast

import pytest
from ai_assistant.adapters.retriever import LEG_OVERFETCH, PostgresRetriever
from ai_assistant.domain.retrieval import Candidate, Filters
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class StubEmbeddings:
    model = "stub"
    dimensions = 3

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


class StubSession:
    """Records every statement and its parameters, and answers each SELECT
    with a scripted row set."""

    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __init__(self, active: str | None = "m:512", rows=None):
        self.active = active
        self.rows = rows if rows is not None else []
        self.statements: list[str] = []
        self.params: list[dict] = []

    async def scalar(self, *_args, **_kwargs):
        return self.active

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append(text)
        self.params.append(params or {})
        if not text.lstrip().upper().startswith("SELECT"):
            return SimpleNamespace(rowcount=0)
        return iter(self.rows)


def _retriever(session: StubSession, embeddings, **kwargs) -> PostgresRetriever:
    """The adapter takes a sessionmaker and owns a session per call, so the
    stub stands in as both — exactly how catalog's search adapter is tested.
    The cast says what the stub is: the slice of AsyncSession this code
    touches, not the whole class."""
    return PostgresRetriever(
        cast(async_sessionmaker[AsyncSession], lambda: session), embeddings, **kwargs
    )


def _row(chunk_id, restaurant_id="r1", item_id="i1"):
    return SimpleNamespace(id=chunk_id, restaurant_id=restaurant_id, item_id=item_id)


def _candidate(chunk_id: str) -> Candidate:
    return Candidate(chunk_id=chunk_id, restaurant_id="r1", item_id=None, score=1.0)


@pytest.fixture
def embeddings():
    return StubEmbeddings()


async def test_the_trigram_threshold_is_applied_to_the_session(embeddings):
    """A fresh connection must not run with pg_trgm's default 0.6, which
    rejects the typos this leg exists to catch."""
    session = StubSession(rows=[])
    await _retriever(session, embeddings).retrieve(query="x", filters=Filters(city="c"), limit=5)
    assert any("pg_trgm.word_similarity_threshold = 0.35" in s for s in session.statements)


async def test_ef_search_is_only_set_when_asked_for(embeddings):
    plain = StubSession(rows=[])
    await _retriever(plain, embeddings).retrieve(query="x", filters=Filters(city="c"), limit=5)
    assert not any("ef_search" in s for s in plain.statements)

    tuned = StubSession(rows=[])
    await _retriever(tuned, embeddings, ef_search=200).retrieve(
        query="x", filters=Filters(city="c"), limit=5
    )
    assert any("SET hnsw.ef_search = 200" in s for s in tuned.statements)


async def test_each_leg_fetches_deeper_than_the_limit_returned(embeddings):
    """Fusion promotes the candidate both legs ranked middling — which only
    exists in the merged set if each leg was asked deep enough to include
    it. Fetching exactly `limit` per leg makes the fusion decorative."""
    session = StubSession(rows=[])
    await _retriever(session, embeddings).retrieve(query="x", filters=Filters(city="c"), limit=10)
    assert all(p["leg_limit"] == 10 * LEG_OVERFETCH for p in session.params if "leg_limit" in p)


async def test_the_query_is_embedded_once_for_all_four_legs(embeddings):
    """Two tables times two legs is four queries and ONE vector: the same
    question embedded four times would be four charges for one answer."""
    session = StubSession(rows=[])
    await _retriever(session, embeddings).retrieve(
        query="something light", filters=Filters(city="c"), limit=5
    )
    assert embeddings.calls == [["something light"]]


async def test_the_vector_is_bound_in_pgvector_text_form(embeddings):
    session = StubSession(rows=[])
    await _retriever(session, embeddings).retrieve(query="x", filters=Filters(city="c"), limit=5)
    bound = next(p["query_vector"] for p in session.params if "query_vector" in p)
    assert bound.startswith("[") and bound.endswith("]")
    assert bound.count(",") == embeddings.dimensions - 1


# ── hydrating the ranked set ────────────────────────────────────────


def _text_row(chunk_id, item_id, content):
    return SimpleNamespace(id=chunk_id, item_id=item_id, content=content)


async def test_the_text_a_model_reads_comes_back_in_RANK_order(embeddings):
    """A bulk read is keyed by id and has no order. Handing the model
    candidates in database order would throw away the ranking the fusion
    just computed — and the model weights what it sees first."""
    session = StubSession(
        rows=[
            _text_row("r1:i2", "i2", "Raita — cooling yoghurt"),
            _text_row("r1:i1", "i1", "Mutton Karahi"),
        ]
    )
    ordered = await _retriever(session, embeddings).texts_in_order(
        [_candidate("r1:i1"), _candidate("r1:i2")]
    )
    assert [(p.item_id, p.restaurant_id, p.text) for p in ordered] == [
        ("i1", "r1", "Mutton Karahi"),
        ("i2", "r1", "Raita — cooling yoghurt"),
    ]


async def test_a_candidate_with_no_row_is_dropped_not_faked(embeddings):
    """A chunk deleted between the ranking and the read has no text. An empty
    string in its place would be a candidate the model cites with nothing
    behind it."""
    session = StubSession(rows=[_text_row("r1:i1", "i1", "Mutton Karahi")])
    ordered = await _retriever(session, embeddings).texts_in_order(
        [_candidate("r1:i1"), _candidate("r1:gone")]
    )
    assert [(p.item_id, p.text) for p in ordered] == [("i1", "Mutton Karahi")]


async def test_hydrating_nothing_costs_no_query(embeddings):
    session = StubSession(rows=[])
    assert await _retriever(session, embeddings).texts_in_order([]) == []
    assert session.statements == []


async def test_the_bulk_read_deduplicates_and_skips_an_empty_set():
    """`texts_for` is the only caller's bulk read: one statement, never one
    per candidate, and no statement at all for nothing."""
    from ai_assistant.adapters.vector_store import PostgresVectorStore

    empty = StubSession()
    store = PostgresVectorStore(cast(AsyncSession, empty))
    assert await store.texts_for(chunk_ids=[]) == {}
    assert empty.statements == []

    session = StubSession(rows=[_text_row("r1:i1", "i1", "Mutton Karahi")])
    found = await PostgresVectorStore(cast(AsyncSession, session)).texts_for(
        chunk_ids=["r1:i1", "r1:i1"]
    )
    assert found == {"r1:i1": ("i1", "Mutton Karahi")}
    assert len(session.statements) == 1
