"""The dishes an answer named, priced live (FR-60, FR-70).

The answer's prose carries no price and no availability — the markers were
stripped before anybody read it, and what survives is a list of item ids on
the message row. This turns those ids into something renderable, at the
moment it is rendered, which is the whole of FR-60: the indexed copy of a
menu is never what a customer is quoted.

Lives here and not in `api/` because it touches adapters, and not in
`domain/` because every line of it is I/O.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.conversations import ConversationRepo
from .adapters.repo import IndexStateRepo
from .adapters.vector_store import PostgresVectorStore
from .domain.combos import minimum_total
from .domain.retrieval import Passage

log = get_logger("ai-assistant.cards")

_SNAPSHOT_MAX = 50
"""Catalog's own cap on `item_ids` per snapshot call, mirrored here.

Pinned rather than imported: it is another service's request contract, and
the failure when it is exceeded is a 422 the client turns into "no cards for
this restaurant" — silent, and worse the more signal a restaurant has.
"""


class CardService:
    """Message id -> cards, or nothing.

    Never raises for a missing dish. An answer that mentioned four dishes
    and can only price three shows three cards and the same prose: the text
    was true when it was written, and a restaurant that has since vanished
    is not a reason to fail the read.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], catalog: Any) -> None:
        self._sessions = sessions
        self._catalog = catalog

    async def for_message(self, *, message_id: str, user_id: str) -> list[dict[str, Any]] | None:
        """None when the message is not this customer's, or does not exist —
        one shape for both, so this cannot be used to discover message ids."""
        async with self._sessions() as session:
            repo = ConversationRepo(session)
            if await repo.owner_of(message_id) != user_id:
                return None
            message = await repo.message(message_id)
            if message is None or not message.item_ids:  # pragma: no cover — owner implies a row
                return []
            active = await IndexStateRepo(session).active()
            if active is None:  # pragma: no cover — an answer implies an index
                return []
            by_restaurant = await PostgresVectorStore(session).restaurants_for(
                item_ids=message.item_ids, model_version=active
            )

        return await self._price(message.item_ids, by_restaurant)

    async def for_items(self, *, passages: Sequence[Passage]) -> list[dict[str, Any]]:
        """Cards for ids that never came from a message (FR-80).

        A recommendation is priced by exactly the same path an answer's
        citations are — one pricing rule, not one per surface. The
        restaurant comes off the passage rather than out of the index,
        because the caller already resolved it.
        """
        by_restaurant = {p.item_id: p.restaurant_id for p in passages}
        return await self._price([p.item_id for p in passages], by_restaurant)

    async def _price(
        self, item_ids: Sequence[str], by_restaurant: dict[str, str]
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[str]] = {}
        for item_id in item_ids:
            restaurant_id = by_restaurant.get(item_id)
            if restaurant_id is not None:
                grouped.setdefault(restaurant_id, []).append(item_id)

        # One request per restaurant, ALL AT ONCE. Sequentially, a single
        # hung catalog multiplied its ~10s worst case by the number of
        # restaurants cited — an answer spanning four kitchens held the
        # customer's browser for ~41s (B3 review). Concurrently the whole
        # read is bounded by the slowest one.
        snapshots = await asyncio.gather(
            *(self._catalog.snapshot(rid, ids) for rid, ids in grouped.items())
        )
        cards: list[dict[str, Any]] = []
        for restaurant_id, snapshot in zip(grouped, snapshots, strict=True):
            if snapshot is None:
                continue
            cards.extend(_cards(restaurant_id, snapshot))
        # Back into the order the ANSWER cited them in: the grouping above
        # is a transport detail, and a customer reading "first the Raita,
        # then the Karahi" should see them in that order.
        rank = {item_id: n for n, item_id in enumerate(item_ids)}
        return sorted(cards, key=lambda card: rank.get(card["item_id"], len(rank)))


def _cards(restaurant_id: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    restaurant = dict(snapshot.get("restaurant", {}))
    # `open_now` is None for a brand and absent on an older catalog; neither
    # means closed, and defaulting to closed would hide every card behind a
    # field that is allowed to be missing.
    orderable = restaurant.get("status") == "open" and restaurant.get("open_now") is not False
    return [
        {
            "item_id": item["id"],
            "name": item["name"],
            "price_cents": item["price_cents"],
            "currency": item["currency"],
            # Two different reasons a card cannot be added, kept apart: the
            # dish is 86'd, or the kitchen is shut. A single "unavailable"
            # would tell a customer to give up on a dish that is back at 6pm.
            "available": bool(item["available"]),
            "orderable": bool(item["available"]) and orderable,
            # A dish with a required choice cannot be added blind: the cart
            # would hold a line the quote endpoint refuses ("group 'Size'
            # requires at least 1 selection"), with no UI anywhere to fix it
            # and deletion the only escape (B3 review). The panel sends
            # these to the restaurant page instead.
            "needs_choice": any(
                int(group.get("min_select", 0)) > 0 for group in item.get("modifier_groups", [])
            ),
            # The least this dish can cost once its REQUIRED options are
            # satisfied — what a budget must be measured against (FR-76),
            # and what a "from $X" label should show. Computed HERE, while
            # the modifier groups still exist: a card carries none, so
            # anything downstream that tried to recompute it would silently
            # read the base price instead.
            "min_total_cents": minimum_total(item),
            "restaurant_id": restaurant_id,
            "restaurant_name": restaurant.get("display_name") or restaurant.get("name", ""),
            "open_now": restaurant.get("open_now") is not False,
        }
        for item in snapshot.get("items", [])
    ]
