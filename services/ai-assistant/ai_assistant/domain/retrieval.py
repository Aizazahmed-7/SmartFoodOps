"""Hybrid retrieval: the predicates, the SQL, and the fusion (FR-62).

Pure and Postgres-only in equal measure, which sounds contradictory and is
not: the FUSION is a pure function over two ranked lists and is tested
exhaustively here, while the SQL is a string this module composes and the
adapter executes. Catalog's `search.py` established the split and the reason
— the SQL cannot run on the sqlite unit suite, so it is proven by shape
(these tests) and by the live smoke, never by pretending sqlite is Postgres.

**Why fuse at all.** Each leg is blind in a way the other is not. The vector
leg cannot match a name it has never seen spelled that way — a customer
typing "biriani" is asking for something whose embedding is nowhere near the
menu's. The lexical leg cannot match "something light" to a salad, because
no salad's text contains the word "light". Ranking them separately and
merging by RANK rather than by score is what lets two incomparable scoring
systems vote without either having to be calibrated against the other.
"""

from collections.abc import Sequence
from dataclasses import dataclass

CONTENT_FTS = "to_tsvector('simple', content)"
"""MUST stay byte-identical to the expression index in migration 0006 —
Postgres only uses an expression index when the query expression matches it
verbatim, and a mismatch degrades silently to a sequential scan. A test pins
this to the migration source."""

WORD_SIMILARITY_THRESHOLD = 0.35
"""The `<%` cutoff, inherited from catalog's live measurement rather than
re-derived: `word_similarity('biriani', 'Biryani House') = 0.45`, and the
default 0.6 rejects it. Set per-connection IN CODE so a fresh environment
can never silently run with the wrong default (ADR-0019)."""

SET_THRESHOLD = f"SET pg_trgm.word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD}"

RRF_K = 60
"""Reciprocal-rank fusion's damping constant, the value from the original
TREC work and the one every implementation since has used unchanged.

What it controls is how steeply rank 1 outranks rank 2. At k=60 the top of
each list is worth a lot but not everything, so an item both legs rank
MIDDLING beats one leg's top hit — which is the entire point of fusing: it
promotes agreement over confidence.
"""


@dataclass(frozen=True)
class Candidate:
    """One retrieved chunk. Ids and a score, never a card.

    Deliberately not a menu item: prices and names are re-resolved live by
    whoever renders this (FR-60), so nothing downstream can accidentally show
    the indexed copy. `restaurant_id` rides along because the caller scopes
    and groups by it without another round trip.
    """

    chunk_id: str
    restaurant_id: str
    item_id: str | None
    score: float


@dataclass(frozen=True)
class Passage:
    """A ranked candidate, hydrated with the text a model reads.

    `restaurant_id` travels with it because the interaction fact needs it
    and the turn is the only place it is knowable for free (FR-94): the
    answer's markers carry item ids, and resolving those back to restaurants
    after the fact is a join analytics cannot do against a menu that has
    since changed. A `Candidate` already knows it — this is the type that
    stops the hydration step from throwing it away.
    """

    item_id: str
    restaurant_id: str
    text: str


@dataclass(frozen=True)
class Hydrated:
    """What the turn's retrieve port returns: the passages AND the vector
    they were found with.

    The vector is not a debugging aid. It is the semantic cache's input
    (FR-74), and handing it out here is what lets that tier cost one indexed
    lookup instead of a second embedding call per turn.
    """

    passages: Sequence[Passage]
    query_vector: Sequence[float] = ()


def fuse(*legs: Sequence[Candidate], k: int = RRF_K, limit: int | None = None) -> list[Candidate]:
    """Reciprocal-rank fusion over any number of ranked legs.

    `score = Σ 1/(k + rank)` across the legs a candidate appears in, ranks
    being 1-based and per-leg.

    Fusing by RANK rather than by score is the load-bearing choice. A cosine
    distance and a `ts_rank` are not on the same scale, do not have the same
    distribution, and are not even monotonic with respect to each other; any
    weighted sum of the two raw numbers is a number with no meaning, and the
    weights would need re-tuning every time either leg changed. Ranks are
    comparable by construction.

    A candidate present in BOTH legs accumulates from both, which is what
    makes agreement between two blind systems the strongest signal available.

    Order is stable: ties break on the candidate's first appearance, so the
    same inputs always produce the same output — a golden set cannot be
    evaluated against a ranking that reshuffles equal scores.
    """
    scores: dict[str, float] = {}
    seen: dict[str, Candidate] = {}
    for leg in legs:
        for rank, candidate in enumerate(leg, start=1):
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (k + rank)
            seen.setdefault(candidate.chunk_id, candidate)
    order = list(seen)  # insertion order = first appearance, for stable ties
    ranked = sorted(order, key=lambda chunk_id: (-scores[chunk_id], order.index(chunk_id)))
    fused = [
        Candidate(
            chunk_id=chunk_id,
            restaurant_id=seen[chunk_id].restaurant_id,
            item_id=seen[chunk_id].item_id,
            score=scores[chunk_id],
        )
        for chunk_id in ranked
    ]
    return fused[:limit] if limit is not None else fused


@dataclass(frozen=True)
class Filters:
    """The hard predicates (FR-62, FR-63). Every one of them is a column on
    the chunk row, because a filter that lives in the prose is a filter the
    database cannot apply.

    `city` has no default and is not optional: every query is geo-scoped, and
    an unscoped retrieval would return dishes from a city the customer cannot
    order from. `open_only` maps to the stored `status`; hours-based
    open/closed is resolved live with price, not from the index.
    """

    city: str
    max_price_cents: int | None = None
    tags: Sequence[str] = ()
    cuisines: Sequence[str] = ()
    available_only: bool = True
    open_only: bool = True


def predicates(filters: Filters, *, items: bool) -> tuple[str, dict[str, object]]:
    """The shared `WHERE` tail and its bound parameters.

    Composed once and used by BOTH legs, which is the property that matters:
    if the lexical and vector legs could drift apart on scoping, one of them
    would eventually return a paused restaurant or another city's menu, and
    the fusion would launder it into the result set as though both agreed.

    There is no generation predicate any more: the embedding model is fixed
    by configuration, so the index holds exactly one vector space and a
    filter on it would always be true.
    """
    clauses = ["city = :city"]
    params: dict[str, object] = {"city": filters.city}
    if filters.open_only:
        clauses.append("status = 'open'")
    if items:
        if filters.available_only:
            clauses.append("available")
        if filters.max_price_cents is not None:
            clauses.append("price_cents <= :max_price_cents")
            params["max_price_cents"] = filters.max_price_cents
        if filters.tags:
            clauses.append("tags @> :tags")
            params["tags"] = list(filters.tags)
    if filters.cuisines:
        clauses.append("cuisines && :cuisines")
        params["cuisines"] = list(filters.cuisines)
    return " AND ".join(clauses), params


ITEM_COLUMNS = "id, restaurant_id, item_id"
RESTAURANT_COLUMNS = "id, restaurant_id, NULL AS item_id"


def _table(items: bool) -> tuple[str, str]:
    return ("item_chunks", ITEM_COLUMNS) if items else ("restaurant_chunks", RESTAURANT_COLUMNS)


def vector_sql(filters: Filters, *, items: bool) -> tuple[str, dict[str, object]]:
    """The semantic leg: nearest neighbours under the hard predicates.

    `<=>` must match the opclass the HNSW index was built with
    (`vector_cosine_ops`, ADR-0032) or the planner ignores the index and
    every search becomes a sequential scan — with no error to notice.

    The filter rides as a WHERE clause rather than a pre-selected id list on
    purpose: pgvector post-filters an ANN scan, so handing it a narrower
    candidate set is not something the query can express. What that costs is
    recall under a very selective filter, which is what `hnsw.ef_search`
    exists to buy back.
    """
    table, columns = _table(items)
    where, params = predicates(filters, items=items)
    sql = (
        f"SELECT {columns} FROM {table} "
        f"WHERE {where} "
        "ORDER BY embedding <=> :query_vector "
        "LIMIT :leg_limit"
    )
    return sql, params


def lexical_sql(filters: Filters, *, items: bool) -> tuple[str, dict[str, object]]:
    """The keyword leg: full text, with a trigram pass for typos.

    Two matchers in one query rather than two legs, because they answer the
    same question at different tolerances — `websearch_to_tsquery` finds the
    words a customer actually typed, `<%` finds the ones they meant. Ordering
    puts exact word matches first: a row matched only by trigram has
    `ts_rank` 0 and sorts below every FTS hit, so a typo never outranks the
    thing it was a typo of.

    `websearch_to_tsquery` rather than `plainto_tsquery` for the same reason
    catalog chose it — customers type quoted phrases and minus signs, and
    websearch syntax is the one that does not raise on them.
    """
    table, columns = _table(items)
    where, params = predicates(filters, items=items)
    fts = f"{CONTENT_FTS} @@ websearch_to_tsquery('simple', :q)"
    sql = (
        f"SELECT {columns} FROM {table} "
        f"WHERE {where} AND ({fts} OR :q <% content) "
        f"ORDER BY ts_rank({CONTENT_FTS}, websearch_to_tsquery('simple', :q)) DESC, "
        "word_similarity(:q, content) DESC "
        "LIMIT :leg_limit"
    )
    return sql, params
