"""The answer cache (FR-74, ADR-0045).

Two tiers, one fence. The tests that matter are not the hits — they are the
cases where a hit would be WRONG: a menu that moved, a different city, a
different embedder, and a question that only means something in the
conversation it was asked in.
"""

import json
from datetime import UTC, datetime
from typing import cast

import pytest
from ai_assistant.adapters.answer_cache import PostgresSemanticCache, RedisExactCache
from ai_assistant.adapters.repo import EpochRepo, IndexStateRepo
from ai_assistant.cache import AnswerCache
from ai_assistant.db import metadata
from ai_assistant.domain.answers import Cached, Fence, cacheable, normalize
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ANSWER = Cached(answer="Try the Raita.", item_ids=["itm_raita"], restaurant_ids=["rst_1"])


# ── normalizing a question ──────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("Something light?", "something light"),
        ("  SOMETHING   LIGHT  ", "something light"),
        ("something light!!", "something light."),
        ("Ｓｏｍｅｔｈｉｎｇ light", "something light"),  # full-width paste
    ],
)
def test_questions_that_are_the_same_question(a: str, b: str):
    assert normalize(a) == normalize(b)


def test_negation_is_not_normalized_away():
    """The one thing an exact tier must never do. Stemming and stop-word
    removal would collide these, and an exact tier that can be wrong is
    worse than no exact tier — the semantic tier takes the fuzzy cases,
    with a threshold and a vector behind it."""
    assert normalize("is the biryani spicy") != normalize("is the biryani not spicy")


# ── the fence ───────────────────────────────────────────────────────


def test_the_fence_is_in_the_key_not_checked_after_the_read():
    """A key that can be read and then rejected is a key that will one day
    be read and NOT rejected. This way a stale entry is unreachable."""
    base = Fence("m:512", "springfield", 3)
    same = base.key("Something light?")
    assert same == Fence("m:512", "springfield", 3).key("something light")
    assert same != Fence("m:512", "springfield", 4).key("something light")  # menu moved
    assert same != Fence("m:512", "karachi", 3).key("something light")  # another city
    assert same != Fence("m:1536", "springfield", 3).key("something light")  # another embedder


# ── what may be cached ──────────────────────────────────────────────


def test_a_reply_is_never_cached():
    """ "What about something spicier?" means nothing without the turn
    before it. No fence catches that — the corpus is fine, the CONTEXT is
    what would be wrong — so history disqualifies the turn outright."""
    assert not cacheable(answer="Try the Raita.", history=[object()], stopped="")


@pytest.mark.parametrize("stopped", ["refused", "no_match"])
def test_a_short_circuited_turn_is_not_cached(stopped: str):
    """A refusal is fixed text and costs no provider call, so caching it
    saves nothing; a no-match risks pinning a stale negative in a city that
    just gained a restaurant."""
    assert not cacheable(answer="anything", history=[], stopped=stopped)


def test_an_empty_answer_is_never_cached():
    assert not cacheable(answer="", history=[], stopped="")


def test_a_plain_answered_question_is_cached():
    assert cacheable(answer="Try the Raita.", history=[], stopped="", item_ids=["itm_raita"])


def test_an_answer_with_a_fabricated_citation_is_never_pinned():
    """ADR-0043 §2 accepts degrading one sentence when a model invents a
    citation. It does not accept serving that sentence to everyone who asks
    the same question for the next hour, from a tier that bypasses
    retrieval and grounding entirely."""
    assert not cacheable(
        answer="Pepperoni Pizza is a great pick.",
        history=[],
        stopped="",
        dropped=1,
        item_ids=[],
    )


def test_an_answer_that_cited_nothing_is_not_cached():
    """Nothing to check it against, and it renders no cards — the one
    answer least worth replaying."""
    assert not cacheable(answer="Sounds tasty.", history=[], stopped="", item_ids=[])


# ── tier 1: exact ───────────────────────────────────────────────────


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int):
        self.store[key] = value
        self.ttls[key] = ex


async def test_the_same_question_comes_back_without_a_query():
    redis = FakeRedis()
    tier = RedisExactCache(redis, ttl_s=3600)
    fence = Fence("m:512", "springfield", 1)
    assert await tier.get(fence=fence, question="Something light?") is None
    await tier.put(fence=fence, question="Something light?", cached=ANSWER)
    hit = await tier.get(fence=fence, question="  something   LIGHT  ")
    assert hit is not None
    assert hit.answer == "Try the Raita."
    assert hit.item_ids == ["itm_raita"] and hit.restaurant_ids == ["rst_1"]


async def test_every_entry_gets_a_ttl():
    """NFR-13's rule, and here it is also the bound on how long a staleness
    the fence does NOT catch survives — a prompt edit, a model swap."""
    redis = FakeRedis()
    await RedisExactCache(redis, ttl_s=900).put(
        fence=Fence("m:512", "springfield", 1), question="q", cached=ANSWER
    )
    assert set(redis.ttls.values()) == {900}


async def test_a_menu_change_makes_the_entry_unreachable():
    redis = FakeRedis()
    tier = RedisExactCache(redis, ttl_s=3600)
    await tier.put(fence=Fence("m:512", "springfield", 1), question="q", cached=ANSWER)
    assert await tier.get(fence=Fence("m:512", "springfield", 2), question="q") is None


async def test_a_cached_answer_carries_ids_and_never_a_price():
    """FR-60 is why an answer is cacheable at all: the client re-resolves
    price and availability live, so the only stale thing a hit can show is
    prose about a dish that still exists."""
    redis = FakeRedis()
    await RedisExactCache(redis, ttl_s=60).put(
        fence=Fence("m:512", "springfield", 1), question="q", cached=ANSWER
    )
    body = json.loads(next(iter(redis.store.values())))
    assert set(body) == {"answer", "item_ids", "restaurant_ids"}


# ── the fence, resolved live ────────────────────────────────────────


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_a_city_that_has_never_been_drained_still_has_a_fence(sessions):
    """1 rather than 0 or None: a fresh deployment needs a usable fence, not
    a missing one that every caller has to special-case."""
    async with sessions() as session:
        assert await EpochRepo(session).current("springfield") == 1


async def test_the_epoch_moves_one_city_at_a_time(sessions):
    async with sessions() as session:
        repo = EpochRepo(session)
        await repo.bump(city="springfield", now=T0)
        await repo.bump(city="springfield", now=T0)
        await session.commit()
    async with sessions() as session:
        repo = EpochRepo(session)
        assert await repo.current("springfield") == 3  # started at 1, bumped twice
        assert await repo.current("karachi") == 1  # untouched


async def test_no_index_means_no_cache(sessions):
    """A cache fenced on a placeholder version would survive the first real
    reindex — the one moment every cached answer becomes meaningless."""
    cache = AnswerCache(sessions, exact_tier=None, semantic_tier=None)
    assert await cache.exact("q", "springfield") == (None, None)
    assert await cache.semantic([0.1], None) is None
    await cache.remember(None, "q", [0.1], ANSWER)  # a no-op, not a crash


async def test_a_write_that_fails_does_not_fail_the_turn(sessions):
    """The answer is already correct and already streaming. A cache write
    that raised would turn a good turn into a failed one to save a future
    turn some latency."""

    class Broken:
        async def put(self, **_kwargs):
            raise RuntimeError("redis is gone")

    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version="m:512", now=T0)
        await session.commit()
    cache = AnswerCache(sessions, exact_tier=Broken(), semantic_tier=Broken())
    await cache.remember(Fence("m:512", "springfield", 1), "q", [0.1], ANSWER)  # no raise


async def test_the_fence_is_resolved_per_lookup_not_captured(sessions):
    """Both halves move while the process is up: the active version under a
    rolling reindex, the epoch on every drain. A fence captured at boot
    would keep serving answers from a corpus that had already changed."""
    seen: list[Fence] = []

    class Spy:
        async def get(self, *, fence: Fence, **_kwargs):
            seen.append(fence)
            return None

    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version="m:512", now=T0)
        await session.commit()
    cache = AnswerCache(sessions, exact_tier=Spy(), semantic_tier=Spy())
    await cache.exact("q", "springfield")
    async with sessions() as session:
        await EpochRepo(session).bump(city="springfield", now=T0)
        await session.commit()
    await cache.exact("q", "springfield")
    assert [f.epoch for f in seen] == [1, 2]


# ── tier 2: semantic ────────────────────────────────────────────────


async def test_the_semantic_tier_is_scoped_to_its_fence(sessions):
    """sqlite has no pgvector, so what is asserted here is everything
    AROUND the distance — the same split catalog's FTS adapter uses. The
    ranking itself is proven by the live smoke."""
    statements: list[str] = []

    class Recorder:
        bind = None

        async def execute(self, statement, *a, **k):
            statements.append(str(statement))

            class Empty:
                def first(self):
                    return None

            return Empty()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    tier = PostgresSemanticCache(
        cast(async_sessionmaker[AsyncSession], lambda: Recorder()), threshold=0.12
    )
    assert await tier.get(fence=Fence("m:512", "springfield", 7), query_vector=[0.1]) is None
    (sql,) = statements
    assert "model_version" in sql and "city" in sql and "epoch" in sql


class Row:
    def __init__(self, distance: float):
        self.d = distance
        self.answer = "Raita, from earlier."
        self.item_ids = ["itm_raita"]
        self.restaurant_ids = ["rst_1"]


def _tier(row, *, threshold: float = 0.12) -> PostgresSemanticCache:
    """The adapter over a stub sessionmaker. The cast says what the stub is:
    the slice of AsyncSession this code touches, not the whole class — the
    same shape `test_retriever` uses for the retrieval SQL."""
    return PostgresSemanticCache(
        cast(async_sessionmaker[AsyncSession], _stub_sessions(row)), threshold=threshold
    )


def _stub_sessions(row):
    class Result:
        def first(self):
            return row

    class Session:
        bind = None

        async def execute(self, *_a, **_k):
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    return lambda: Session()


async def test_a_near_enough_question_is_served(sessions):
    tier = _tier(Row(0.05))
    hit = await tier.get(fence=Fence("m:512", "springfield", 1), query_vector=[0.1])
    assert hit is not None
    assert hit.answer == "Raita, from earlier." and hit.item_ids == ["itm_raita"]


async def test_the_nearest_question_can_still_be_too_far(sessions):
    """The threshold is checked on the ROW rather than in the WHERE clause:
    an ANN index orders by distance and does not filter by it, so a
    predicate would be applied after the scan anyway — and reading the
    actual distance is what makes the cut tunable against real data."""
    tier = _tier(Row(0.4))
    assert await tier.get(fence=Fence("m:512", "springfield", 1), query_vector=[0.1]) is None


async def test_asking_the_same_thing_twice_overwrites_rather_than_accumulates(sessions):
    """Keyed by the normalized question and the model version, so a re-ask
    replaces the row — otherwise a popular question grows a row per asking
    and the ANN scan gets slower the more useful the cache becomes."""
    import sqlalchemy as sa
    from ai_assistant.db import answer_cache as table

    tier = PostgresSemanticCache(sessions, threshold=0.12)
    fence = Fence("m:512", "springfield", 1)
    for answer in ("first", "second"):
        await tier.put(
            fence=fence,
            question="Something light?",
            query_vector=[0.1, 0.2],
            cached=Cached(answer=answer, item_ids=["itm_raita"], restaurant_ids=["rst_1"]),
            now=T0,
        )
    async with sessions() as session:
        rows = (await session.execute(sa.select(table.c.answer, table.c.epoch))).all()
    assert [r.answer for r in rows] == ["second"]


async def test_a_menu_change_writes_a_new_row_under_the_new_fence(sessions):
    """The fence rides as columns rather than in the key, so a re-ask after
    a menu change replaces the entry and the old epoch simply stops
    matching — no sweep, no delete."""
    import sqlalchemy as sa
    from ai_assistant.db import answer_cache as table

    tier = PostgresSemanticCache(sessions, threshold=0.12)
    for epoch in (1, 2):
        await tier.put(
            fence=Fence("m:512", "springfield", epoch),
            question="Something light?",
            query_vector=[0.1],
            cached=ANSWER,
            now=T0,
        )
    async with sessions() as session:
        epochs = (await session.execute(sa.select(table.c.epoch))).scalars().all()
    assert epochs == [2]


async def test_the_row_id_is_the_question_not_a_surrogate():
    """Writing the same question twice must COLLIDE, not accumulate."""
    assert Fence("m", "c", 1).row_id("Something light?") == Fence("m", "c", 9).row_id(
        "  something  LIGHT "
    )


async def test_the_service_writes_both_tiers_and_reads_the_semantic_one(sessions):
    """The wiring proof: one fence resolved once, handed to both tiers."""
    seen: dict[str, Fence] = {}

    class Tier:
        def __init__(self, name: str):
            self.name = name
            self.written = False

        async def get(self, *, fence: Fence, **_kwargs):
            seen[self.name] = fence
            return ANSWER

        async def put(self, *, fence: Fence, **_kwargs):
            seen[self.name] = fence
            self.written = True

    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version="m:512", now=T0)
        await EpochRepo(session).bump(city="springfield", now=T0)
        await session.commit()

    exact_tier, semantic_tier = Tier("exact"), Tier("semantic")
    cache = AnswerCache(sessions, exact_tier=exact_tier, semantic_tier=semantic_tier)

    fence = Fence("m:512", "springfield", 2)
    assert (await cache.semantic([0.1, 0.2], fence)) == ANSWER
    assert seen["semantic"] == fence

    await cache.remember(fence, "q", [0.1, 0.2], ANSWER)
    assert exact_tier.written and semantic_tier.written


async def test_an_answer_with_no_vector_is_written_to_the_exact_tier_only(sessions):
    """An exact HIT never retrieves, so a turn can settle without ever
    producing a vector. Writing the semantic tier with an empty embedding
    would put a row in the ANN index that matches nothing and ranks first
    against anything."""
    written: list[str] = []

    class Tier:
        def __init__(self, name: str):
            self.name = name

        async def put(self, **_kwargs):
            written.append(self.name)

    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version="m:512", now=T0)
        await session.commit()
    cache = AnswerCache(sessions, exact_tier=Tier("exact"), semantic_tier=Tier("semantic"))
    await cache.remember(Fence("m:512", "springfield", 1), "q", [], ANSWER)
    assert written == ["exact"]


def test_the_cache_is_all_or_nothing():
    """An exact tier without Redis is not a degraded cache — it is a lookup
    that always misses plus a write that always fails."""
    from ai_assistant.main import _answer_cache

    from .conftest import settings

    assert _answer_cache(settings(), None, None) is None
    assert _answer_cache(settings(answer_cache="off"), None, object()) is None
    assert _answer_cache(settings(), None, object()) is not None


# ── failing open, for real ──────────────────────────────────────────


async def test_a_corrupt_entry_is_a_miss_not_a_crash():
    """The `try` used to cover only the Redis call, and the hit was counted
    before the parse — so an entry written by an older build raised a
    KeyError, counted as a hit, and failed the customer's turn."""

    class Corrupt:
        async def get(self, _key: str):
            return '{"not_an_answer": 1}'

    tier = RedisExactCache(Corrupt(), ttl_s=60)
    assert await tier.get(fence=Fence("m:512", "springfield", 1), question="q") is None


async def test_a_dead_redis_is_a_miss_not_a_crash():
    class Dead:
        async def get(self, _key: str):
            raise RuntimeError("connection reset")

        async def set(self, *_a, **_k):
            raise RuntimeError("connection reset")

    tier = RedisExactCache(Dead(), ttl_s=60)
    fence = Fence("m:512", "springfield", 1)
    assert await tier.get(fence=fence, question="q") is None
    await tier.put(fence=fence, question="q", cached=ANSWER)  # no raise


async def test_a_failing_semantic_read_is_a_miss_not_a_failed_turn(sessions):
    """Retrieval has already succeeded by the time this runs, so a pool
    timeout here was turning a question that would have generated fine into
    "Sorry, I couldn't finish that" (B3 review)."""

    class Broken:
        bind = None

        async def execute(self, *_a, **_k):
            raise RuntimeError("statement timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    tier = PostgresSemanticCache(
        cast(async_sessionmaker[AsyncSession], lambda: Broken()), threshold=0.12
    )
    assert await tier.get(fence=Fence("m:512", "springfield", 1), query_vector=[0.1]) is None


async def test_the_read_is_bounded_by_age_as_well_as_the_fence(sessions):
    """The fence covers corpus changes; the age bound covers every change it
    cannot see. `created_at` was written and never read, so in a city whose
    menus are quiet nothing ever aged out and a prompt fix would never
    reach it (B3 review)."""
    statements: list[str] = []

    class Recorder:
        bind = None

        async def execute(self, statement, *a, **k):
            statements.append(str(statement))

            class Empty:
                def first(self):
                    return None

            return Empty()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    tier = PostgresSemanticCache(
        cast(async_sessionmaker[AsyncSession], lambda: Recorder()), threshold=0.12
    )
    await tier.get(fence=Fence("m:512", "springfield", 1), query_vector=[0.1])
    assert "created_at" in statements[0]
