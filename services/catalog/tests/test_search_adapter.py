"""PostgresSearch branches: query building (filter permutations), the
index-expression pin, merge semantics, and the four-leg execution flow
against a stub session (real-PG matching is proven by the live smoke)."""

from pathlib import Path
from types import SimpleNamespace

from catalog.adapters.search import (
    ITEM_FTS,
    RESTAURANT_FTS,
    PostgresSearch,
    build_queries,
    merge,
)


def test_fts_expressions_are_pinned_to_the_migration():
    """PG only uses an expression index when the query expression matches it
    VERBATIM — drift here silently degrades every search to a seq scan."""
    migration = (
        Path(__file__).parent.parent / "migrations" / "versions" / "0001_initial.py"
    ).read_text()
    assert RESTAURANT_FTS in migration
    assert ITEM_FTS in migration


def test_build_queries_filter_permutations():
    none = build_queries(None, None, None)
    assert ":city" not in none["restaurants"]
    assert "rc.cuisine" not in none["restaurants"]

    full = build_queries("springfield", "pakistani", "halal")
    for leg in full.values():  # filters constrain every leg identically
        assert "r.city = :city" in leg
        assert "rc.cuisine = :cuisine" in leg
        assert "it.tag = :tag AND mi.available" in leg

    # Fuzzy + FTS present where they belong:
    assert "word_similarity(:q, r.name)" in full["restaurants"]
    assert "websearch_to_tsquery" in full["items"]
    assert ":q <% rc2.cuisine" in full["cuisines"]
    assert ":q <% it2.tag" in full["tags"]
    # Alias-injected expressions still match the pinned index expressions:
    assert RESTAURANT_FTS.replace("name", "r.name") in full["restaurants"]
    assert (
        ITEM_FTS.replace("name", "i.name", 1).replace("description", "i.description")
        in full["items"]
    )


def _r(restaurant_id, score):
    return SimpleNamespace(restaurant_id=restaurant_id, score=score)


def _i(restaurant_id, item_id, score, name="Item", price=100):
    return SimpleNamespace(
        restaurant_id=restaurant_id, id=item_id, score=score, name=name, price_cents=price
    )


def test_merge_takes_best_score_and_dedupes_items():
    hits = merge(
        [_r("rst_a", 0.4), _r("rst_a", 0.6)],  # same restaurant, two legs
        [
            _i("rst_a", "itm_1", 0.3),
            _i("rst_a", "itm_1", 0.9),  # same item, better score in another leg
            _i("rst_b", "itm_2", 0.5),
        ],
        limit=10,
        offset=0,
    )
    assert [h["restaurant_id"] for h in hits] == ["rst_a", "rst_b"]
    assert hits[0]["score"] == 0.9  # item hit outranked the restaurant hits
    assert len(hits[0]["matched_items"]) == 1  # deduped
    assert hits[0]["matched_items"][0]["score"] == 0.9  # kept the best
    assert hits[1]["score"] == 0.5


def test_merge_ranks_and_paginates():
    restaurant_hits = [_r(f"rst_{n}", n / 10) for n in range(5)]
    page = merge(restaurant_hits, [], limit=2, offset=2)
    assert [h["restaurant_id"] for h in page] == ["rst_2", "rst_1"]  # desc, 3rd+4th
    assert merge([], [], limit=2, offset=0) == []
    assert merge([_r("rst_x", None)], [], limit=2, offset=0)[0]["score"] == 0.0  # null-safe


class _StubResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _StubSession:
    """Routes each leg's SQL to canned rows via distinctive substrings."""

    def __init__(self, by_marker):
        self.by_marker = by_marker
        self.params_seen = []

    async def execute(self, stmt, params=None):
        self.params_seen.append(params)
        sql = str(stmt)
        for marker, rows in self.by_marker.items():
            if marker in sql:
                return _StubResult(rows)
        return _StubResult([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_search_executes_all_legs_and_merges():
    session = _StubSession(
        {
            "word_similarity(:q, r.name": [_r("rst_a", 0.8)],
            "rc2.cuisine": [_r("rst_b", 0.4)],
            "word_similarity(:q, i.name": [_i("rst_a", "itm_1", 0.6, name="Biryani", price=1200)],
            "it2.tag": [_i("rst_c", "itm_2", 0.3, name="Salad", price=500)],
        }
    )
    adapter = PostgresSearch(lambda: session)
    hits = await adapter.search(
        query="biryani", city="springfield", cuisine=None, tag=None, limit=10, offset=0
    )
    assert len(session.params_seen) == 5  # threshold SET + every leg
    assert session.params_seen[0] is None  # the SET carries no params
    assert session.params_seen[1] == {"q": "biryani", "city": "springfield"}
    assert [h["restaurant_id"] for h in hits] == ["rst_a", "rst_b", "rst_c"]
    assert hits[0]["matched_items"][0]["name"] == "Biryani"


async def test_search_all_filters_bind_params():
    session = _StubSession({})
    adapter = PostgresSearch(lambda: session)
    assert (
        await adapter.search(
            query="q!", city="c", cuisine="k", tag="t", limit=5, offset=5
        )
        == []
    )
    assert session.params_seen[1] == {"q": "q!", "city": "c", "cuisine": "k", "tag": "t"}
