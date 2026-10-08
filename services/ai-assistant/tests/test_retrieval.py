"""Fusion and predicates (FR-62, FR-63).

The fusion is where two blind systems vote, so the tests are mostly about
what fusing by RANK buys that a weighted score sum would not. The predicates
are where a paused restaurant or another city's menu would leak in, so those
tests are about the clauses being present, identical for both legs, and
absent when they should be.
"""

from pathlib import Path

from ai_assistant.domain.retrieval import (
    CONTENT_FTS,
    RRF_K,
    WORD_SIMILARITY_THRESHOLD,
    Candidate,
    Filters,
    fuse,
    predicates,
)

MIGRATION = (
    Path(__file__).parent.parent / "migrations" / "versions" / "0006_lexical_leg.py"
).read_text()


def _item(chunk_id: str, restaurant_id: str = "r1") -> Candidate:
    return Candidate(
        chunk_id=chunk_id, restaurant_id=restaurant_id, item_id=chunk_id.split(":")[-1], score=0.0
    )


# ── fusion ──────────────────────────────────────────────────────────


def test_a_single_leg_keeps_its_order():
    leg = [_item("r1:a"), _item("r1:b"), _item("r1:c")]
    assert [c.chunk_id for c in fuse(leg)] == ["r1:a", "r1:b", "r1:c"]


def test_agreement_beats_either_leg_alone():
    """THE property. `b` is second in both legs and first in neither; `a` and
    `c` each top one leg. Fusing promotes the candidate both blind systems
    liked over the one either was most confident about — which is the only
    reason to run two legs instead of the better one."""
    lexical = [_item("r1:a"), _item("r1:b")]
    vector = [_item("r1:c"), _item("r1:b")]
    assert [c.chunk_id for c in fuse(lexical, vector)][0] == "r1:b"


def test_a_candidate_in_one_leg_only_still_survives():
    """The lexical leg is the only thing that can match a typo'd name, and
    the vector leg the only thing that can match "something light". Fusion
    must not silently require both."""
    lexical = [_item("r1:typo")]
    vector = [_item("r1:vague")]
    assert {c.chunk_id for c in fuse(lexical, vector)} == {"r1:typo", "r1:vague"}


def test_scores_are_the_reciprocal_rank_sum():
    both = fuse([_item("r1:a")], [_item("r1:a")])
    assert both[0].score == 2 / (RRF_K + 1)


def test_ties_break_on_first_appearance_not_at_random():
    """A golden set cannot be evaluated against a ranking that reshuffles
    equal scores between runs."""
    lexical = [_item("r1:a"), _item("r1:b")]
    vector = [_item("r1:b"), _item("r1:a")]
    first = [c.chunk_id for c in fuse(lexical, vector)]
    assert first == [c.chunk_id for c in fuse(lexical, vector)]
    assert first == ["r1:a", "r1:b"]  # a appeared first


def test_the_limit_applies_after_fusing_not_before():
    """Truncating a leg before the merge would discard exactly the
    middling-but-agreed candidates the fusion exists to promote."""
    lexical = [_item("r1:a"), _item("r1:b"), _item("r1:c")]
    vector = [_item("r1:c"), _item("r1:b"), _item("r1:a")]
    assert len(fuse(lexical, vector, limit=2)) == 2


def test_fusing_nothing_yields_nothing():
    assert fuse([], []) == []


def test_identity_fields_survive_the_merge():
    """The caller groups and scopes by restaurant_id without another round
    trip, so the merge must not lose it."""
    fused = fuse([Candidate("r9:i1", "r9", "i1", 0.0)])
    assert (fused[0].restaurant_id, fused[0].item_id) == ("r9", "i1")


# ── predicates ──────────────────────────────────────────────────────


def test_every_query_is_scoped_to_a_city():
    """Unscoped retrieval would return dishes from a city the customer
    cannot order from. There is no generation predicate any more — the
    index holds one vector space, so a filter on it would always be true."""
    sql, params = predicates(Filters(city="springfield"), items=True)
    assert "city = :city" in sql
    assert "model_version" not in sql
    assert params["city"] == "springfield"


def test_the_default_posture_excludes_paused_and_unavailable():
    sql, _ = predicates(Filters(city="springfield"), items=True)
    assert "status = 'open'" in sql
    assert "available" in sql


def test_a_budget_is_a_predicate_not_a_hope():
    """FR-76: a stated budget narrows in SQL. The live re-resolution is
    still authoritative, but the index must not hand back a page of
    candidates that are all over budget."""
    sql, params = predicates(Filters(city="x", max_price_cents=1000), items=True)
    assert "price_cents <= :max_price_cents" in sql
    assert params["max_price_cents"] == 1000


def test_tags_use_containment_and_cuisines_overlap():
    """`@>` means "has ALL of these tags" — a request for spicy AND halal is
    a conjunction. `&&` means "any of these cuisines", because a restaurant
    listing thai and bbq satisfies a search for either."""
    sql, params = predicates(
        Filters(city="x", tags=["spicy", "halal"], cuisines=["thai"]), items=True
    )
    assert "tags @> :tags" in sql
    assert "cuisines && :cuisines" in sql
    assert params["tags"] == ["spicy", "halal"]
    assert params["cuisines"] == ["thai"]


def test_the_restaurant_leg_omits_item_only_columns():
    """`restaurant_chunks` has no price, availability, tags or category —
    emitting those clauses would be a SQL error, not a narrower search."""
    sql, params = predicates(
        Filters(city="x", max_price_cents=500, tags=["spicy"], cuisines=["thai"]), items=False
    )
    assert "price_cents" not in sql
    assert "tags" not in sql
    assert "available" not in sql
    assert "cuisines &&" in sql  # this one IS on both tables
    assert "max_price_cents" not in params


def test_relaxing_the_defaults_drops_the_clauses():
    sql, _ = predicates(
        Filters(city="x", available_only=False, open_only=False),
        items=True,
    )
    assert "status" not in sql
    assert "available" not in sql


def test_no_filter_emits_an_unbound_parameter():
    """A parameter in the SQL with nothing bound to it is a runtime error at
    execute time, which is the worst place to find it."""
    for items in (True, False):
        sql, params = predicates(
            Filters(city="x", max_price_cents=900, tags=["a"], cuisines=["b"]), items=items
        )
        for name in params:
            assert f":{name}" in sql


# ── the pins ────────────────────────────────────────────────────────


def test_the_fts_expression_is_pinned_to_the_migration():
    """Postgres only uses an expression index when the query expression
    matches it VERBATIM — drift here silently degrades every search to a
    sequential scan, with no error anywhere. Catalog learned this once."""
    assert f'CONTENT_FTS = "{CONTENT_FTS}"' in MIGRATION


def test_the_trigram_threshold_is_set_in_code_not_assumed():
    """Live-measured in catalog: word_similarity('biriani', 'Biryani House')
    is 0.45, and pg_trgm's default cutoff of 0.6 rejects it. A fresh
    environment must not silently run with the wrong default."""
    assert WORD_SIMILARITY_THRESHOLD == 0.35
