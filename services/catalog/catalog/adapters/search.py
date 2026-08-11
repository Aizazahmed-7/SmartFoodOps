"""PostgreSQL implementation of SearchPort (ADR-0019).

Four ranked candidate legs — restaurant names, item names/descriptions,
cuisine tags, item tags — each combining full-text (websearch_to_tsquery)
with per-word trigram fuzziness (`<%` / word_similarity — plain similarity
dilutes across multi-word names and misses "biriani"→"Biryani House"; the
live smoke caught exactly that), then
merged, deduped, and paginated in-process.

The FTS expression constants below MUST stay byte-identical to the
expression indexes in migrations/versions/0001_initial.py — PostgreSQL only
uses an expression index when the query expression matches it verbatim.
A test pins this file against the migration source.

This module is pure-PG SQL, so its execute calls run against a stub session
in the unit suite; matching quality is proven by the live smoke (slice 8).
"""

from collections import defaultdict

import sqlalchemy as sa

RESTAURANT_FTS = "to_tsvector('simple', name)"
ITEM_FTS = "to_tsvector('simple', name || ' ' || coalesce(description, ''))"

# Per-leg candidate cap before the merge. Far above any real page; if a query
# ever legitimately exceeds it, ranking (not correctness) degrades.
_CAP = 200

# The <% operator's cutoff. Default 0.6 rejects honest typos — live-measured:
# word_similarity('biriani', 'Biryani House') = 0.45. At 0.35 that matches
# while unrelated words (~0.1-0.2) stay out. Set per-connection IN CODE so a
# fresh environment can never silently run with the wrong default.
WORD_SIMILARITY_THRESHOLD = 0.35
_SET_THRESHOLD = f"SET pg_trgm.word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD}"


def _filters(city: str | None, cuisine: str | None, tag: str | None) -> str:
    clauses = []
    if city is not None:
        clauses.append("AND r.city = :city")
    if cuisine is not None:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM restaurant_cuisines rc "
            "WHERE rc.restaurant_id = r.id AND rc.cuisine = :cuisine)"
        )
    if tag is not None:
        clauses.append(
            "AND EXISTS (SELECT 1 FROM menu_items mi "
            "JOIN item_tags it ON it.item_id = mi.id "
            "WHERE mi.restaurant_id = r.id AND it.tag = :tag AND mi.available)"
        )
    return " ".join(clauses)


def build_queries(city: str | None, cuisine: str | None, tag: str | None) -> dict[str, str]:
    f = _filters(city, cuisine, tag)
    rest_fts = RESTAURANT_FTS.replace("name", "r.name")
    item_fts = ITEM_FTS.replace("name", "i.name", 1).replace("description", "i.description")
    return {
        "restaurants": f"""
            SELECT r.id AS restaurant_id,
                   GREATEST(ts_rank({rest_fts}, websearch_to_tsquery('simple', :q)),
                            word_similarity(:q, r.name)) AS score
            FROM restaurants r
            WHERE ({rest_fts} @@ websearch_to_tsquery('simple', :q) OR :q <% r.name)
            {f} ORDER BY score DESC LIMIT {_CAP}""",
        "cuisines": f"""
            SELECT r.id AS restaurant_id, word_similarity(:q, rc2.cuisine) AS score
            FROM restaurants r
            JOIN restaurant_cuisines rc2 ON rc2.restaurant_id = r.id
            WHERE :q <% rc2.cuisine
            {f} ORDER BY score DESC LIMIT {_CAP}""",
        "items": f"""
            SELECT i.restaurant_id, i.id, i.name, i.price_cents,
                   GREATEST(ts_rank({item_fts}, websearch_to_tsquery('simple', :q)),
                            word_similarity(:q, i.name)) AS score
            FROM menu_items i
            JOIN restaurants r ON r.id = i.restaurant_id
            WHERE ({item_fts} @@ websearch_to_tsquery('simple', :q) OR :q <% i.name)
              AND i.available
            {f} ORDER BY score DESC LIMIT {_CAP}""",
        "tags": f"""
            SELECT i.restaurant_id, i.id, i.name, i.price_cents,
                   word_similarity(:q, it2.tag) AS score
            FROM item_tags it2
            JOIN menu_items i ON i.id = it2.item_id
            JOIN restaurants r ON r.id = i.restaurant_id
            WHERE :q <% it2.tag AND i.available
            {f} ORDER BY score DESC LIMIT {_CAP}""",
    }


def merge(restaurant_hits, item_hits, limit: int, offset: int) -> list[dict]:
    """Collapse legs: a restaurant's score is its best hit anywhere; items
    dedupe across legs keeping the best score; rank desc, then paginate."""
    scores: dict[str, float] = {}
    matched: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in restaurant_hits:
        score = float(row.score or 0.0)
        scores[row.restaurant_id] = max(scores.get(row.restaurant_id, 0.0), score)
    for row in item_hits:
        score = float(row.score or 0.0)
        scores[row.restaurant_id] = max(scores.get(row.restaurant_id, 0.0), score)
        best = matched[row.restaurant_id].get(row.id)
        if best is None or best["score"] < score:
            matched[row.restaurant_id][row.id] = {
                "id": row.id,
                "name": row.name,
                "price_cents": row.price_cents,
                "score": score,
            }
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {
            "restaurant_id": restaurant_id,
            "score": score,
            "matched_items": sorted(
                matched[restaurant_id].values(), key=lambda i: (-i["score"], i["id"])
            ),
        }
        for restaurant_id, score in ranked[offset : offset + limit]
    ]


class PostgresSearch:
    def __init__(self, sessions):
        self._sessions = sessions

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        cuisine: str | None,
        tag: str | None,
        limit: int,
        offset: int,
    ) -> list[dict]:
        params: dict = {"q": query}
        if city is not None:
            params["city"] = city
        if cuisine is not None:
            params["cuisine"] = cuisine
        if tag is not None:
            params["tag"] = tag
        legs = build_queries(city, cuisine, tag)
        async with self._sessions() as session:
            await session.execute(sa.text(_SET_THRESHOLD))
            restaurants = (await session.execute(sa.text(legs["restaurants"]), params)).all()
            cuisines = (await session.execute(sa.text(legs["cuisines"]), params)).all()
            items = (await session.execute(sa.text(legs["items"]), params)).all()
            tags = (await session.execute(sa.text(legs["tags"]), params)).all()
        return merge(restaurants + cuisines, items + tags, limit, offset)
