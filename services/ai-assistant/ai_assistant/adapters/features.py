"""Order features: the write from Kafka, and the popularity read (FR-80).

Aggregates are computed HERE, at read time, by grouping the fact rows —
never materialised back as counters. A counter cannot absorb at-least-once
redelivery: `count = count + 1` applied twice is a lie and no natural key
saves an increment, which is precisely why these are facts.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import item_chunks, menu_views, order_items, restaurant_chunks, taste_profiles
from ..domain.popularity import WINDOW, Popular, hours_around
from ..domain.taste import Attributes, Taste


class FeatureRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    async def record(self, rows: Sequence[dict[str, Any]]) -> None:
        """One statement per batch, DO NOTHING on the pair.

        DO NOTHING rather than DO UPDATE because an order's items cannot
        change — the order's lifecycle is somebody else's table. It also
        makes duplicate keys legal WITHIN one statement, which a batch
        spanning a replayed partition routinely has.
        """
        if not rows:
            return
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        await self._s.execute(
            insert(order_items)
            .values(list(rows))
            .on_conflict_do_nothing(index_elements=["order_id", "item_id"])
        )

    async def record_views(self, rows: Sequence[dict[str, Any]]) -> None:
        """DO NOTHING on the emitter-minted `view_id` — telemetry, deduped
        by the same natural key as everything else."""
        if not rows:
            return
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        await self._s.execute(
            insert(menu_views).values(list(rows)).on_conflict_do_nothing(index_elements=["view_id"])
        )

    async def users_with_history(self, *, since: datetime) -> list[str]:
        """Who the builder has work to do for.

        Bounded by the same window the profile is: a customer who stopped
        ordering six months ago has no profile to rebuild, and walking every
        user who ever ordered would make the job grow forever.
        """
        rows = await self._s.execute(
            sa.select(sa.distinct(order_items.c.user_id)).where(order_items.c.placed_at >= since)
        )
        return [row[0] for row in rows]

    async def taste_rows(
        self, *, user_id: str, since: datetime, model_version: str
    ) -> list[tuple[str, str, str, Sequence[str], Sequence[str], int]]:
        """One customer's ordered dishes, joined to the attributes a profile
        is made of.

        Joined to the INDEX rather than to the order payload: the payload
        knows a name as it was sold, the index knows the tags and cuisines a
        restaurant declared — and those are what a content-based profile is
        built from. A dish since removed from the menu contributes nothing,
        which is correct: it cannot be recommended either.
        """
        rows = await self._s.execute(
            sa.select(
                order_items.c.order_id,
                order_items.c.item_id,
                order_items.c.restaurant_id,
                item_chunks.c.cuisines,
                item_chunks.c.tags,
                order_items.c.qty,
            )
            .select_from(
                order_items.join(
                    item_chunks,
                    (item_chunks.c.item_id == order_items.c.item_id)
                    & (item_chunks.c.restaurant_id == order_items.c.restaurant_id)
                    & (item_chunks.c.model_version == model_version),
                )
            )
            .where(order_items.c.user_id == user_id, order_items.c.placed_at >= since)
        )
        return [
            (
                r.order_id,
                r.item_id,
                r.restaurant_id,
                list(r.cuisines or ()),
                list(r.tags or ()),
                int(r.qty),
            )
            for r in rows
        ]

    async def viewed_restaurants(self, *, user_id: str, since: datetime) -> list[str]:
        """Restaurants this customer browsed — FR-75's second input.

        One entry per VIEW, not per restaurant: somebody who opened the same
        menu six times is six times more interested than somebody who opened
        it once, and the profile counts them the same way it counts orders.
        """
        rows = await self._s.execute(
            sa.select(menu_views.c.restaurant_id).where(
                menu_views.c.user_id == user_id, menu_views.c.viewed_at >= since
            )
        )
        return [row[0] for row in rows]

    async def save_profile(self, *, user_id: str, profile: Taste, now: datetime) -> None:
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        values = {
            "user_id": user_id,
            "cuisines": dict(profile.cuisines),
            "tags": dict(profile.tags),
            "restaurants": dict(profile.restaurants),
            "ordered": list(profile.ordered),
            "orders": profile.orders,
            "built_at": now,
        }
        stmt = insert(taste_profiles).values(**values)
        await self._s.execute(
            stmt.on_conflict_do_update(
                index_elements=[taste_profiles.c.user_id],
                set_={k: v for k, v in values.items() if k != "user_id"},
            )
        )

    async def profile(self, user_id: str) -> Taste | None:
        row = (
            await self._s.execute(
                sa.select(taste_profiles).where(taste_profiles.c.user_id == user_id)
            )
        ).first()
        if row is None:
            return None
        return Taste(
            cuisines=dict(row.cuisines or {}),
            tags=dict(row.tags or {}),
            restaurants=dict(row.restaurants or {}),
            ordered=tuple(row.ordered or ()),
            orders=int(row.orders),
        )

    async def menu_attributes(self, *, city: str, model_version: str) -> list[Attributes]:
        """Every dish a city can offer, as scorable attributes.

        The whole city, because a recommendation is a ranking over what is
        available — not a filter over what was retrieved. A query would be a
        different feature; this one runs before the customer has typed
        anything at all.
        """
        rows = await self._s.execute(
            sa.select(
                item_chunks.c.item_id,
                item_chunks.c.restaurant_id,
                item_chunks.c.cuisines,
                item_chunks.c.tags,
            ).where(
                item_chunks.c.city == city,
                item_chunks.c.model_version == model_version,
                item_chunks.c.available.is_(True),
                item_chunks.c.status == "open",
            )
        )
        return [
            Attributes(
                item_id=r.item_id,
                restaurant_id=r.restaurant_id,
                cuisines=list(r.cuisines or ()),
                tags=list(r.tags or ()),
            )
            for r in rows
        ]

    async def co_ordered(
        self, *, city: str, since: datetime, model_version: str, limit: int = 50
    ) -> list[tuple[str, str, str, int]]:
        """`(restaurant_id, item_a, item_b, orders together)` — dishes people
        buy in one go, and where.

        A self-join on `order_id`, with `a.item_id < b.item_id` doing two
        jobs: it drops the self-pair a join always produces, and it makes
        each pair appear once instead of twice in both orders.

        **Within one restaurant**, as FR-77 requires, and that is a
        correctness constraint rather than a nicety: the cart holds one
        restaurant per order, so a "combo" spanning two kitchens is a
        suggestion the customer cannot act on.

        The restaurant is GROUPED BY and returned, not merely joined on.
        Grouping by the dish pair alone merged a brand's branches into one
        count and then left the caller to resolve each half independently —
        which, since one `item_id` is served by every branch (ADR-0028),
        produced combos spanning two branches that no cart could hold
        (B4 review).
        """
        in_city = (
            sa.select(restaurant_chunks.c.restaurant_id)
            .where(
                restaurant_chunks.c.city == city,
                restaurant_chunks.c.model_version == model_version,
            )
            .scalar_subquery()
        )
        other = sa.alias(order_items, "other")
        rows = await self._s.execute(
            sa.select(
                order_items.c.restaurant_id.label("rst"),
                order_items.c.item_id.label("a"),
                other.c.item_id.label("b"),
                sa.func.count(sa.distinct(order_items.c.order_id)).label("orders"),
            )
            .select_from(
                order_items.join(
                    other,
                    (other.c.order_id == order_items.c.order_id)
                    & (other.c.restaurant_id == order_items.c.restaurant_id)
                    & (order_items.c.item_id < other.c.item_id),
                )
            )
            .where(
                order_items.c.restaurant_id.in_(in_city),
                order_items.c.placed_at >= since,
            )
            .group_by(order_items.c.restaurant_id, order_items.c.item_id, other.c.item_id)
            .order_by(sa.func.count(sa.distinct(order_items.c.order_id)).desc())
            .limit(limit)
        )
        return [(row.rst, row.a, row.b, int(row.orders)) for row in rows]

    async def any_in(self, *, city: str, model_version: str, limit: int) -> list[str]:
        """Any orderable dish in the city, cheapest first.

        FR-80's floor beneath popularity. A brand-new city has no order
        history at all, and at 03:00 even a busy one has nothing inside the
        ±1h band over 30 days — so the panel that promises "never an empty
        response" returned nothing, on the one endpoint whose docstring
        makes that promise (B4 review).

        Cheapest first because this is the fallback nobody chose: with no
        signal to rank on, the least presumptuous order is by price, and it
        is also the one most likely to survive a budget.
        """
        rows = await self._s.execute(
            sa.select(item_chunks.c.item_id)
            .where(
                item_chunks.c.city == city,
                item_chunks.c.model_version == model_version,
                item_chunks.c.available.is_(True),
                item_chunks.c.status == "open",
            )
            .order_by(item_chunks.c.price_cents, item_chunks.c.item_id)
            .limit(limit)
        )
        return list(dict.fromkeys(row.item_id for row in rows))

    async def popular_in(self, *, city: str, at: datetime, model_version: str) -> list[Popular]:
        """What this city orders around this hour.

        Scoped by RESTAURANT, not by item: the index carries one chunk per
        (restaurant, item), so joining on items would count a dish once per
        branch that sells it and let a widely-franchised base dish outrank
        everything by arithmetic. Restaurants are the thing a city actually
        has.

        The city mapping comes from our own index rather than from the order
        payload, so "city" means exactly what it means in retrieval (FR-63)
        — the restaurant's city, not the delivery address's. Two different
        notions, and mixing them would make a recommendation disagree with
        the search results beside it.
        """
        in_city = (
            sa.select(restaurant_chunks.c.restaurant_id)
            .where(
                restaurant_chunks.c.city == city,
                restaurant_chunks.c.model_version == model_version,
            )
            .scalar_subquery()
        )
        rows = await self._s.execute(
            sa.select(
                order_items.c.item_id,
                sa.func.count(sa.distinct(order_items.c.order_id)).label("orders"),
                sa.func.sum(order_items.c.qty).label("units"),
            )
            .where(
                order_items.c.restaurant_id.in_(in_city),
                order_items.c.placed_at >= at - WINDOW,
                _hour_of(order_items.c.placed_at, self._dialect).in_(
                    # ZERO-PADDED: `strftime('%H')` returns "07", and
                    # comparing it against "7" matched nothing — so on
                    # sqlite the whole 00:00-09:59 half of the day was dead.
                    # The dialect split exists so the band would be tested,
                    # and every fixture used noon, the one hour where an
                    # unpadded string happens to be two digits (B4 review).
                    [f"{h:02d}" for h in hours_around(at)]
                    if self._dialect == "sqlite"
                    else hours_around(at)
                ),
            )
            .group_by(order_items.c.item_id)
        )
        return [
            Popular(item_id=row.item_id, orders=int(row.orders), units=int(row.units))
            for row in rows
        ]


def _hour_of(column: Any, dialect: str) -> Any:
    """Hour-of-day, in whichever dialect is under us.

    sqlite has no `EXTRACT`, and the unit suite runs on sqlite — so without
    this split the time-of-day band would be tested nowhere and shipped
    untested, which is the shape of every dialect bug this project has had.
    `strftime` returns text, hence the string comparison above.
    """
    if dialect == "sqlite":
        return sa.func.strftime("%H", column)
    return sa.extract("hour", column)
