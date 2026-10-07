"""A restaurant's own numbers, for copy about its business (FR-89, FR-90).

FR-90 is explicit: engagement copy is drafted from the restaurant's own
aggregates and contains no customer PII. This module is where that promise
is kept, and it is kept structurally — every value returned is a COUNT or a
dish name. No user ids, no order ids, no addresses, no dates of individual
orders. There is nothing here for a model to leak because nothing
identifying is ever loaded.

Aggregates are computed at READ time by grouping `order_items`, which is
the facts-not-counters rule B4 established: a counter cannot absorb
at-least-once redelivery, and these numbers end up in copy a restaurant
sends to its customers.

Scoped by claim, like every other read in the studio — a brand may ask
about any of its branches, and nobody may ask about anyone else's.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import IndexStateRepo
from .db import item_chunks, order_items

WINDOW_DAYS = 90
"""How far back "recently" reaches. A quarter is long enough that a
restaurant with a few orders a day has something to say, and short enough
that the numbers describe the business as it is now."""

LAPSED_AFTER_DAYS = 45
"""A customer who has not ordered in six weeks, having ordered before.

An arbitrary line, and labelled as one: it is in the facts handed to the
model so the copy can say "in the last six weeks" rather than implying a
precision nobody has.
"""

TOP_DISHES = 3


@dataclass(frozen=True)
class RestaurantFacts:
    """What this restaurant's own data supports saying about it.

    Every field is a count or a dish name. That is the FR-90 guarantee,
    and it is a property of the type rather than of the prompt.
    """

    orders: int
    customers: int
    repeat_customers: int
    lapsed_customers: int
    top_dishes: list[str]
    window_days: int = WINDOW_DAYS
    lapsed_after_days: int = LAPSED_AFTER_DAYS

    def as_subject(self) -> dict[str, Any]:
        return {
            "orders": self.orders,
            "customers": self.customers,
            "repeat_customers": self.repeat_customers,
            "lapsed_customers": self.lapsed_customers,
            "top_dishes": list(self.top_dishes),
            "window_days": self.window_days,
            "lapsed_after_days": self.lapsed_after_days,
        }

    @property
    def thin(self) -> bool:
        """Too little to write from.

        A restaurant with two orders has no aggregate worth describing, and
        copy written from one would be a claim dressed as a statistic. The
        caller refuses rather than drafting — the same floor FR-92 puts
        under feedback summaries.
        """
        return self.orders < 5


class RestaurantFactsReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _active(self, session: AsyncSession) -> str | None:
        """The generation retrieval is actually reading, resolved per call.

               NOT the configured one. Every other index reader in this service
               asks `IndexStateRepo` for the same reason: an operator who changes
               `embedding_model` and restarts leaves the old generation active
               until a reindex finishes, and a reader pinned to config would query
               a version with no rows. For the studio that showed up as "N not
               found on this menu" against the admin's own menu — and worse, as a
               promotion quoting an undercounted order total, which is precisely
               the fabricated statistic this reader exists to prevent.
        has been built yet — no rows to read, and
               certainly not a licence to read every generation at once.
        """
        return await IndexStateRepo(session).active()

    async def for_restaurant(self, claim: str, *, now: datetime | None = None) -> RestaurantFacts:
        now = now or datetime.now(UTC)
        since = now - timedelta(days=WINDOW_DAYS)
        lapsed_before = now - timedelta(days=LAPSED_AFTER_DAYS)

        # The branches this claim owns. Resolved from the index rather than
        # assumed, because a claim may name a BRAND (ADR-0028) and
        # `order_items` only knows branches.
        async with self._sessions() as session:
            version = await self._active(session)
            if version is None:
                return RestaurantFacts(0, 0, 0, 0, [])
            branches = [
                row.restaurant_id
                for row in (
                    await session.execute(
                        sa.select(item_chunks.c.restaurant_id)
                        .where(
                            sa.and_(
                                item_chunks.c.model_version == version,
                                sa.or_(
                                    item_chunks.c.restaurant_id == claim,
                                    item_chunks.c.brand_id == claim,
                                ),
                            )
                        )
                        .distinct()
                    )
                ).all()
            ]
            if not branches:
                return RestaurantFacts(0, 0, 0, 0, [])

            mine = sa.and_(
                order_items.c.restaurant_id.in_(branches),
                order_items.c.placed_at >= since,
            )

            totals = (
                await session.execute(
                    sa.select(
                        sa.func.count(sa.distinct(order_items.c.order_id)),
                        sa.func.count(sa.distinct(order_items.c.user_id)),
                    ).where(mine)
                )
            ).one()

            # Per-customer order counts, aggregated and then COUNTED — the
            # user ids never leave the database.
            per_customer = (
                sa.select(
                    order_items.c.user_id.label("user_id"),
                    sa.func.count(sa.distinct(order_items.c.order_id)).label("orders"),
                    sa.func.max(order_items.c.placed_at).label("last_order"),
                )
                .where(mine)
                .group_by(order_items.c.user_id)
                .subquery()
            )
            repeat = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(per_customer)
                    .where(per_customer.c.orders > 1)
                )
            ).scalar_one()
            lapsed = (
                await session.execute(
                    sa.select(sa.func.count())
                    .select_from(per_customer)
                    .where(per_customer.c.last_order < lapsed_before)
                )
            ).scalar_one()

            top = (
                await session.execute(
                    sa.select(order_items.c.item_id, sa.func.sum(order_items.c.qty).label("sold"))
                    .where(mine)
                    .group_by(order_items.c.item_id)
                    .order_by(sa.desc("sold"), order_items.c.item_id)
                    .limit(TOP_DISHES)
                )
            ).all()
            names = await self._names([row.item_id for row in top], branches, session, version)

        return RestaurantFacts(
            orders=int(totals[0] or 0),
            customers=int(totals[1] or 0),
            repeat_customers=int(repeat or 0),
            lapsed_customers=int(lapsed or 0),
            top_dishes=names,
        )

    async def _names(
        self, item_ids: list[str], branches: list[str], session: AsyncSession, version: str
    ) -> list[str]:
        """Dish names, in the order the ids were given.

        Scoped to the same branches: a dish id that is not theirs resolves
        to nothing rather than to someone else's menu.
        """
        if not item_ids:
            return []
        rows = (
            await session.execute(
                sa.select(item_chunks.c.item_id, item_chunks.c.name).where(
                    sa.and_(
                        item_chunks.c.model_version == version,
                        item_chunks.c.restaurant_id.in_(branches),
                        item_chunks.c.item_id.in_(item_ids),
                    )
                )
            )
        ).all()
        by_id = {row.item_id: row.name for row in rows}
        return [by_id[item_id] for item_id in item_ids if item_id in by_id]
