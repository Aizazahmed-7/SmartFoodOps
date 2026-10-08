"""Recommendations, for the graph to fall back to (FR-80).

Lives here and not in `domain/` because every line of it is I/O, and not in
`api/` because the layer contract forbids routes touching adapters. What it
adds over `FeatureRepo` is hydration: popularity is a list of ids, and the
turn needs passages.
"""

from collections.abc import Sequence
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.attribution import AttributionRepo
from .adapters.features import FeatureRepo
from .adapters.vector_store import PostgresVectorStore
from .domain.popularity import WINDOW, rank
from .domain.retrieval import Passage
from .domain.taste import recommend
from .metrics import RECOMMENDATIONS

log = get_logger("ai-assistant.recommend")


class Recommender:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record_shown(
        self, *, user_id: str, city: str, surface: str, basis: str, item_ids: Any
    ) -> str | None:
        """Note what a customer was just offered (FR-79).

        Its own transaction, and best-effort: the recommendation is already
        computed and about to be rendered, so failing to record that we
        showed it must not cost the customer the list. What it costs is one
        missing denominator in the acceptance rate, which is the right way
        round.
        """
        from datetime import UTC, datetime  # noqa: PLC0415 — a clock, not a dependency

        try:
            async with self._sessions() as session:
                shown_id = await AttributionRepo(session).record_shown(
                    user_id=user_id,
                    city=city,
                    surface=surface,
                    basis=basis,
                    item_ids=list(item_ids),
                    now=datetime.now(UTC),
                )
                await session.commit()
                if shown_id is not None:
                    RECOMMENDATIONS.labels(surface=surface, outcome="shown").inc()
                return shown_id
        except Exception as exc:
            # Counted, not just logged: a failure here removes a whole
            # surface from the acceptance denominator, and the rate goes on
            # looking healthy because nothing reads the tables (B4 review).
            RECOMMENDATIONS.labels(surface=surface, outcome="unrecorded").inc()
            log.warning("recommendation not recorded", error=str(exc))
            return None

    async def pairs_in(self, *, city: str, limit: int = 200) -> list[tuple[str, str, str, int]]:
        """Raw co-order signal: `(item_a, item_b, orders together)`.

        Returned unpriced, because the caller is the only one that can price
        it — and pricing is what decides which pairs survive a budget.

        The limit is generous for the same reason the dish list over-fetches
        under a budget: the SQL cannot see the budget, so taking the top 50
        pairs and then filtering returned NOTHING in a city whose most
        co-ordered pairs are all mains — while affordable pairs sat just
        below the cut (B4 review).
        """
        from datetime import UTC, datetime  # noqa: PLC0415 — a clock, not a dependency

        async with self._sessions() as session:
            return await FeatureRepo(session).co_ordered(
                city=city,
                since=datetime.now(UTC) - WINDOW,
                limit=limit,
            )

    async def goes_with(self, *, item_ids: Sequence[str], city: str, limit: int) -> list[Passage]:
        """What people order ALONGSIDE these dishes (FR-78).

        Read off the same co-order signal combos use, but asked the other
        way round: not "which pairs are popular" but "given this dish, what
        comes with it". The partners are returned as passages so the turn
        can cite them like any other candidate — a recommendation the model
        cannot ground is a recommendation it should not make.

        Ranked by how often the pairing actually happened, so the answer is
        "people order naan with this" rather than "naan is a bread".
        """
        wanted = set(item_ids)
        if not wanted:
            return []
        partners: dict[tuple[str, str], int] = {}
        for restaurant_id, first, second, orders in await self.pairs_in(city=city):
            # Each pair is canonical (`a < b`), so both ends have to be
            # checked — the dish asked about can be on either side.
            for anchor, partner in ((first, second), (second, first)):
                if anchor in wanted and partner not in wanted:
                    key = (restaurant_id, partner)
                    partners[key] = max(partners.get(key, 0), orders)
        ordered = sorted(partners, key=lambda key: (-partners[key], key[1]))[:limit]
        return await self.hydrate(ordered)

    async def hydrate(self, pinned: Sequence[tuple[str, str]]) -> list[Passage]:
        """`(restaurant_id, item_id)` -> passages.

        PINNED, because one `item_id` is served by every branch of a brand
        (ADR-0028) and letting it resolve freely put a co-ordered pair's two
        halves in two different branches — a combo no cart could hold.
        """
        wanted = list(dict.fromkeys(pinned))
        if not wanted:
            return []
        async with self._sessions() as session:
            store = PostgresVectorStore(session)
            found: list[Passage] = []
            for restaurant_id, item_id in wanted:
                texts = await store.texts_by_item(
                    item_ids=[item_id],
                    restaurant_id=restaurant_id,
                )
                if item_id in texts:
                    found.append(
                        Passage(
                            item_id=item_id,
                            restaurant_id=restaurant_id,
                            text=texts[item_id][1],
                        )
                    )
        return found

    async def for_user(
        self, *, user_id: str, city: str, limit: int, at: Any = None
    ) -> tuple[str, list[Passage]]:
        """`(basis, dishes)` — personalised when there is enough history,
        the popularity baseline when there is not (FR-75, FR-80).

        `basis` travels out because FR-75's acceptance criterion is a
        COMPARISON: a personalised list must differ measurably from the
        baseline, and neither the caller nor the eval suite can check that
        without knowing which one it got.
        """
        async with self._sessions() as session:
            repo = FeatureRepo(session)
            profile = await repo.profile(user_id)
            if profile is None or profile.thin:
                # One order is a data point, not a preference. A
                # "personalised" list built off it would differ from the
                # baseline with nothing behind the difference, which is
                # worse than the baseline because it looks like it knows
                # something.
                return "popular", await self.popular(city=city, limit=limit, at=at)
            candidates = await repo.menu_attributes(city=city)
        if not candidates:
            return "popular", await self.popular(city=city, limit=limit, at=at)
        # Deduped BEFORE ranking too: `menu_attributes` yields one row per
        # (restaurant, item), so a six-branch brand contributed six
        # identical candidates and `recommend` happily returned all six.
        unique: dict[str, Any] = {}
        for candidate in candidates:
            unique.setdefault(candidate.item_id, candidate)
        chosen = recommend(profile, list(unique.values()), limit)
        return "taste", await self._passages([c.item_id for c in chosen], city=city)

    async def _passages(self, item_ids: list[str], *, city: str) -> list[Passage]:
        """Ranked ids -> passages, DEDUPED and scoped to the city.

        Deduped because the index holds one chunk per (restaurant, item), so
        a brand with six branches offered the same dish six times and it
        filled every recommendation slot. Scoped because without a city a
        dish could resolve to a branch in another one (B4 review).
        """
        async with self._sessions() as session:
            found = await PostgresVectorStore(session).texts_by_item(item_ids=item_ids, city=city)
        seen: set[str] = set()
        passages: list[Passage] = []
        for item_id in item_ids:
            if item_id in found and item_id not in seen:
                seen.add(item_id)
                passages.append(
                    Passage(
                        item_id=item_id,
                        restaurant_id=found[item_id][0],
                        text=found[item_id][1],
                    )
                )
        return passages

    async def popular(self, *, city: str, limit: int, at: Any = None) -> list[Passage]:
        """The city's most-ordered dishes around this hour, as passages.

        Hydrated from the INDEX, not from the order history: the history
        knows an id and a name as it was sold, the index knows the text this
        service is willing to show. A dish ordered last month and taken off
        the menu since therefore drops out here rather than being
        recommended from a name nobody can order — which is the same reason
        `texts_in_order` exists on the retrieval path.
        """
        from datetime import UTC, datetime  # noqa: PLC0415 — a clock, not a dependency

        when = at or datetime.now(UTC)
        async with self._sessions() as session:
            repo = FeatureRepo(session)
            counted = await repo.popular_in(city=city, at=when)
            ordered = rank(counted, limit)
            if not ordered:
                # FR-80's floor: a city with no order history in this hour
                # band still has a menu, and "never an empty response" is
                # the requirement this endpoint exists for.
                fallback = await repo.any_in(city=city, limit=limit)
                return await self._passages(fallback, city=city)
            found = await PostgresVectorStore(session).texts_by_item(
                item_ids=[p.item_id for p in ordered], city=city
            )
        seen: set[str] = set()
        passages: list[Passage] = []
        for popular in ordered:
            if popular.item_id in found and popular.item_id not in seen:
                seen.add(popular.item_id)
                restaurant_id, text = found[popular.item_id]
                passages.append(
                    Passage(item_id=popular.item_id, restaurant_id=restaurant_id, text=text)
                )
        return passages
