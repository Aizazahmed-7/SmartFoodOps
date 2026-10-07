"""`SearchPort` backed by the assistant's hybrid retriever, falling back to
`PostgresSearch` (FR-65, ADR-0019, ADR-0029 §4).

This is the ONE read-path call Part A makes into the GenAI plane, and the
whole design of it is about that being safe. ADR-0029 permits it on exactly
three conditions, all enforced here rather than remembered:

- **flag-gated** — `hybrid_search=off` is a config change, not a deploy
- **timeout-bounded** — search is on a customer's critical path; ADR-0019
  gave it a p99 of 150 ms and a semantic leg cannot be allowed to spend more
- **falls back** — every failure lands on `PostgresSearch`, so killing the
  assistant degrades search from semantic to lexical rather than breaking it

The port's shape is unchanged, which is the point: `CatalogService` cannot
tell which implementation it holds, and the day OpenSearch replaces either
one it still cannot.

**Ids in, cards out.** The assistant returns ranked `item_id`s and nothing
else; names and prices are read HERE, from catalog's own tables, at request
time. That is not a convenience — it is what makes it structurally
impossible for the index's up-to-60s-stale `price_cents` to reach a
customer through search, rather than a rule someone has to keep applying.
"""

from typing import Any

import httpx
from smartfood_auth import internal_headers
from smartfood_otel import get_logger

from ..domain.ports import SEARCH_PATH, SearchPort
from .repo import CatalogRepo

log = get_logger("catalog.hybrid-search")


class HybridSearch:
    def __init__(
        self,
        sessions: Any,
        fallback: SearchPort,
        *,
        base_url: str,
        http: httpx.AsyncClient,
        timeout_s: float = 0.15,
        enabled: bool = False,
    ) -> None:
        self._sessions = sessions
        self._fallback = fallback
        self._url = base_url.rstrip("/") + "/v1/internal/assistant/retrieve"
        self._http = http
        self._timeout_s = timeout_s
        self._enabled = enabled

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        cuisine: str | None,
        tag: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        SEARCH_PATH.set("lexical")
        if not self._enabled or city is None:
            # An unscoped search cannot use the semantic path at all: every
            # retrieval is geo-scoped (FR-63), and a city-less query would
            # have to either invent a city or drop the scope. Lexical search
            # has no such constraint, so it keeps answering.
            return await self._fallback.search(
                query=query, city=city, cuisine=cuisine, tag=tag, limit=limit, offset=offset
            )
        candidates = await self._retrieve(query, city, cuisine, tag, limit + offset)
        if candidates is None:
            return await self._fallback.search(
                query=query, city=city, cuisine=cuisine, tag=tag, limit=limit, offset=offset
            )
        SEARCH_PATH.set("hybrid")
        return await self._hydrate(candidates, limit=limit, offset=offset)

    async def _retrieve(
        self, query: str, city: str, cuisine: str | None, tag: str | None, depth: int
    ) -> dict[str, Any] | None:
        """The assistant's ranking, or `None` meaning "use the fallback".

        Every failure is the same answer — timeout, connection refused, 503,
        a 500, a body that does not parse. The caller has one sane response
        to all of them and it is not to care which happened, so they collapse
        here rather than upward. The log line keeps them distinguishable for
        whoever is debugging.

        An EMPTY result is not a failure and does not fall back: "nothing
        matches" is a legitimate answer, and treating it as an outage would
        make the flag meaningless the first time someone searched for
        something the catalog does not sell.
        """
        body: dict[str, Any] = {
            "query": query,
            "city": city,
            "limit": min(depth, 50),
            # Match `PostgresSearch` exactly. Its results include paused
            # restaurants — the card carries `status` so the client can badge
            # one — and a fallback that returns a DIFFERENT set from the
            # primary is not a fallback, it is a second product behind a
            # flag. The assistant's own surfaces keep the strict default.
            "open_only": False,
        }
        if tag:
            body["tags"] = [tag]
        if cuisine:
            body["cuisines"] = [cuisine]
        try:
            response = await self._http.post(
                self._url, json=body, headers=internal_headers("catalog"), timeout=self._timeout_s
            )
            if response.status_code >= 300:
                log.warning("hybrid search degraded", status=response.status_code)
                return None
            return response.json()
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            log.warning("hybrid search degraded", error=type(exc).__name__)
            return None

    async def _hydrate(
        self, candidates: dict[str, Any], *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """Ranked ids -> the cards `SearchPort` promises, priced from OUR
        tables at THIS moment.

        Restaurants keep the order the fusion gave them: a hit's position is
        the retriever's judgement, and re-sorting by anything available here
        would be this adapter substituting its own.
        """
        items = list(candidates.get("items") or [])
        restaurants = list(candidates.get("restaurants") or [])

        order: list[str] = []
        scores: dict[str, float] = {}
        matched: dict[str, list[dict[str, Any]]] = {}
        for candidate in items + restaurants:
            restaurant_id = candidate["restaurant_id"]
            if restaurant_id not in scores:
                order.append(restaurant_id)
                matched[restaurant_id] = []
            # The best evidence wins rather than the sum: a restaurant with
            # nine weak matches is not a better answer than one with a
            # single excellent dish, and adding scores would say it is.
            scores[restaurant_id] = max(scores.get(restaurant_id, 0.0), candidate["score"])

        item_ids = [c["item_id"] for c in items if c.get("item_id")]
        async with self._sessions() as session:
            priced = await CatalogRepo(session).get_items_for_search(item_ids)
        by_id = {row.id: row for row in priced}

        for candidate in items:
            row = by_id.get(candidate["item_id"])
            if row is None:
                # Deleted between the index being written and this read. The
                # index is a cache of what EXISTED; catalog is the truth
                # about what exists now, and the truth wins silently.
                continue
            matched[candidate["restaurant_id"]].append(
                {
                    "id": row.id,
                    "name": row.name,
                    "price_cents": row.price_cents,
                    "score": candidate["score"],
                }
            )

        hits = [
            {
                "restaurant_id": restaurant_id,
                "score": scores[restaurant_id],
                "matched_items": matched[restaurant_id],
            }
            for restaurant_id in order
        ]
        return hits[offset : offset + limit]
