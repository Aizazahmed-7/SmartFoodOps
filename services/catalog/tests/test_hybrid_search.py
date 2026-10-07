"""`HybridSearch` (FR-65, ADR-0029 §4).

Every test here is a variation on one question: when the GenAI plane
misbehaves, does `/v1/search` still answer? That is B2's exit criterion and
the condition ADR-0029 attached to letting a Part A read path call the AI
plane at all.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from catalog.adapters.hybrid_search import HybridSearch
from catalog.domain.ports import SEARCH_PATH


class FallbackSearch:
    """Stands in for PostgresSearch; records whether it was consulted."""

    def __init__(self):
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return [{"restaurant_id": "rst_lexical", "score": 1.0, "matched_items": []}]


class StubSession:
    def __init__(self, rows=()):
        self.rows = rows

    async def execute(self, *_args, **_kwargs):
        return iter(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _row(item_id, name="Chicken Karahi", price_cents=899):
    return SimpleNamespace(id=item_id, name=name, price_cents=price_cents)


def _adapter(handler, fallback=None, rows=(), **kwargs) -> tuple[HybridSearch, FallbackSearch]:
    fallback = fallback or FallbackSearch()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = StubSession(rows)
    adapter = HybridSearch(
        lambda: session,
        fallback,
        base_url="http://assistant:8013",
        http=http,
        enabled=kwargs.pop("enabled", True),
        **kwargs,
    )
    return adapter, fallback


def _ok(payload):
    return lambda request: httpx.Response(200, json=payload)


async def _search(adapter, **over):
    kwargs = {
        "query": "biryani",
        "city": "springfield",
        "cuisine": None,
        "tag": None,
        "limit": 10,
        "offset": 0,
    }
    kwargs.update(over)
    return await adapter.search(**kwargs)


# ── degradation: the reason this adapter is allowed to exist ────────


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, json={"error": {"code": "DEPENDENCY_UNAVAILABLE"}}),
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(200, text="not json"),
    ],
    ids=["unavailable", "server_error", "unparseable"],
)
async def test_every_assistant_failure_degrades_to_lexical(handler):
    """One answer for all of them. The caller has a single sane response to
    a timeout, a refusal, a 500 and a mangled body, and it is not to care
    which happened."""
    adapter, fallback = _adapter(handler)
    hits = await _search(adapter)
    assert hits == [{"restaurant_id": "rst_lexical", "score": 1.0, "matched_items": []}]
    assert len(fallback.calls) == 1


async def test_a_connection_refused_degrades_to_lexical():
    """THE exit criterion: kill the assistant, search still answers."""

    def refuse(request):
        raise httpx.ConnectError("connection refused")

    adapter, fallback = _adapter(refuse)
    assert await _search(adapter) == [
        {"restaurant_id": "rst_lexical", "score": 1.0, "matched_items": []}
    ]
    assert len(fallback.calls) == 1


async def test_a_timeout_degrades_to_lexical():
    """Search is on a customer's critical path with a 150 ms p99 budget
    (ADR-0019). Past that, the lexical answer is already better than a
    slower semantic one."""

    def slow(request):
        raise httpx.ReadTimeout("too slow")

    adapter, fallback = _adapter(slow)
    await _search(adapter)
    assert len(fallback.calls) == 1


async def test_the_flag_off_never_calls_the_assistant():
    def explode(request):  # pragma: no cover — must not be reached
        raise AssertionError("the assistant was called with the flag off")

    adapter, fallback = _adapter(explode, enabled=False)
    await _search(adapter)
    assert len(fallback.calls) == 1


async def test_a_city_less_search_stays_lexical():
    """Every retrieval is geo-scoped (FR-63). A city-less query would have
    to invent a city or drop the scope; lexical search has no such
    constraint, so it keeps answering."""
    adapter, fallback = _adapter(_ok({"items": [], "restaurants": []}))
    await _search(adapter, city=None)
    assert len(fallback.calls) == 1


async def test_no_results_is_an_answer_not_an_outage():
    """Falling back on empty would make the flag meaningless the first time
    somebody searched for something the catalog does not sell."""
    adapter, fallback = _adapter(_ok({"items": [], "restaurants": []}))
    assert await _search(adapter) == []
    assert fallback.calls == []


# ── hydration: ids in, live cards out ───────────────────────────────


async def test_names_and_prices_come_from_catalog_not_the_index():
    """The index's copy is up to a debounce window stale. Reading here is
    what makes it structurally unable to reach a customer."""
    adapter, _ = _adapter(
        _ok(
            {
                "items": [
                    {"chunk_id": "r1:i1", "restaurant_id": "r1", "item_id": "i1", "score": 0.03}
                ],
                "restaurants": [],
            }
        ),
        rows=[_row("i1", name="Chicken Karahi", price_cents=1299)],
    )
    hits = await _search(adapter)
    assert hits[0]["matched_items"] == [
        {"id": "i1", "name": "Chicken Karahi", "price_cents": 1299, "score": 0.03}
    ]


async def test_an_item_deleted_since_indexing_is_dropped_silently():
    """The index is a cache of what EXISTED; catalog is the truth about what
    exists now, and the truth wins."""
    adapter, _ = _adapter(
        _ok(
            {
                "items": [
                    {"chunk_id": "r1:gone", "restaurant_id": "r1", "item_id": "gone", "score": 0.03}
                ],
                "restaurants": [],
            }
        ),
        rows=[],
    )
    hits = await _search(adapter)
    assert hits[0]["matched_items"] == []


async def test_a_restaurant_hit_needs_no_items():
    """`SearchPort` promises restaurants matched DIRECTLY as well as
    restaurants surfaced via items."""
    adapter, _ = _adapter(
        _ok(
            {
                "items": [],
                "restaurants": [
                    {"chunk_id": "r1:_self", "restaurant_id": "r1", "item_id": None, "score": 0.01}
                ],
            }
        )
    )
    hits = await _search(adapter)
    assert hits == [{"restaurant_id": "r1", "score": 0.01, "matched_items": []}]


async def test_a_restaurants_score_is_its_best_hit_not_its_total():
    """Nine weak matches are not a better answer than one excellent dish,
    and summing would say they are."""
    adapter, _ = _adapter(
        _ok(
            {
                "items": [
                    {"chunk_id": "r1:i1", "restaurant_id": "r1", "item_id": "i1", "score": 0.01},
                    {"chunk_id": "r1:i2", "restaurant_id": "r1", "item_id": "i2", "score": 0.03},
                ],
                "restaurants": [],
            }
        ),
        rows=[_row("i1"), _row("i2")],
    )
    hits = await _search(adapter)
    assert hits[0]["score"] == 0.03


async def test_retrieval_order_is_preserved():
    """A hit's position is the retriever's judgement; re-sorting here would
    be this adapter substituting its own."""
    adapter, _ = _adapter(
        _ok(
            {
                "items": [
                    {"chunk_id": "r2:i2", "restaurant_id": "r2", "item_id": "i2", "score": 0.03},
                    {"chunk_id": "r1:i1", "restaurant_id": "r1", "item_id": "i1", "score": 0.02},
                ],
                "restaurants": [],
            }
        ),
        rows=[_row("i1"), _row("i2")],
    )
    assert [h["restaurant_id"] for h in await _search(adapter)] == ["r2", "r1"]


async def test_the_filters_are_forwarded():
    seen: dict = {}

    def capture(request):
        seen.update(
            httpx.Response(200).json() if False else __import__("json").loads(request.content)
        )
        return httpx.Response(200, json={"items": [], "restaurants": []})

    adapter, _ = _adapter(capture)
    await _search(adapter, tag="halal", cuisine="thai", limit=5, offset=5)
    assert seen["tags"] == ["halal"]
    assert seen["cuisines"] == ["thai"]
    assert seen["city"] == "springfield"
    assert seen["limit"] == 10  # limit + offset: the page has to be reachable


async def test_pagination_slices_the_ranked_list():
    items = [
        {"chunk_id": f"r{i}:i{i}", "restaurant_id": f"r{i}", "item_id": f"i{i}", "score": 0.1}
        for i in range(4)
    ]
    adapter, _ = _adapter(
        _ok({"items": items, "restaurants": []}), rows=[_row(f"i{i}") for i in range(4)]
    )
    hits = await _search(adapter, limit=2, offset=2)
    assert [h["restaurant_id"] for h in hits] == ["r2", "r3"]


async def test_the_fallback_and_the_primary_return_the_same_universe():
    """A fallback that returns a DIFFERENT set from the primary is not a
    fallback, it is a second product behind a flag. PostgresSearch includes
    paused restaurants (the card carries `status` for exactly that), so the
    hybrid leg must ask for them too."""
    seen: dict = {}

    def capture(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"items": [], "restaurants": []})

    adapter, _ = _adapter(capture)
    await _search(adapter)
    assert seen["open_only"] is False


async def test_the_path_that_answered_is_recorded():
    """`/v1/search` returns 200 with plausible results either way, so "the
    results look fine" does not mean the retriever ran. The eval suite
    refuses to score a response that is not stamped `hybrid`, after once
    grading the fallback and reporting it as the semantic path."""
    adapter, _ = _adapter(_ok({"items": [], "restaurants": []}))
    await _search(adapter)
    assert SEARCH_PATH.get() == "hybrid"

    def refuse(request):
        raise httpx.ConnectError("refused")

    degraded, _ = _adapter(refuse)
    await _search(degraded)
    assert SEARCH_PATH.get() == "lexical"


async def test_the_flag_off_records_lexical():
    adapter, _ = _adapter(_ok({"items": [], "restaurants": []}), enabled=False)
    await _search(adapter)
    assert SEARCH_PATH.get() == "lexical"


async def test_concurrent_searches_do_not_see_each_others_path():
    """The reason this is a ContextVar and not an attribute. The adapter is
    built once and shared, so an attribute would let a fallback running
    beside a semantic search overwrite its value before the route read it —
    and the header would confidently report the wrong path."""

    def refuse(request):
        raise httpx.ConnectError("refused")

    good, _ = _adapter(_ok({"items": [], "restaurants": []}))
    bad, _ = _adapter(refuse)

    async def observe(adapter):
        await _search(adapter)
        return SEARCH_PATH.get()

    # Separate tasks get separate contexts, which is exactly the property a
    # shared attribute would not have.
    paths = await asyncio.gather(
        asyncio.create_task(observe(good)), asyncio.create_task(observe(bad))
    )
    assert paths == ["hybrid", "lexical"]
