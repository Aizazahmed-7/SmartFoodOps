"""The facts a menu draft is written FROM (FR-88).

UC-25 names them exactly: item name, tags, category, cuisine. All four are
columns on `item_chunks`, so this is one local read rather than a
cross-service call from a worker — and it is scoped by restaurant in the
WHERE clause, which is also what validates that the items the admin asked
about are theirs. An id belonging to someone else simply does not come
back, so there is no second ownership check to forget.

**Deliberately the index, not the catalog.** The index is up to one debounce
window stale (NFR-28), which for a money path would be disqualifying and
here is not: a draft is reviewed by a human before it reaches a customer
(FR-93), and the facts are frozen into the draft row, so a reviewer sees
exactly what the copy was written from. The alternative — one authoritative
catalog read per item from inside a Celery worker — buys freshness nobody
can act on and costs a cross-service fan-out.

What is NOT here is as deliberate: no price, no availability. Copy that
mentions a price is wrong the next time anyone edits it, and ADR-0033 keeps
both out of the embedded text for the same reason.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import item_chunks

MAX_ITEMS = 50
"""The fan-out cap for one request. Matches catalog's snapshot bound, and
exists for the same reason: "draft my whole menu" is a legitimate thing to
want and an illegitimate thing to do in one request."""


@dataclass(frozen=True)
class MenuFacts:
    """One dish, as copy should be written about it."""

    item_id: str
    # The BRANCH the dish belongs to, which a brand claim may differ from.
    # The draft row records the branch, because copy is for a menu and a
    # menu belongs to a place.
    restaurant_id: str
    brand_id: str | None
    name: str
    category: str
    tags: Sequence[str]
    cuisines: Sequence[str]

    def as_subject(self) -> dict[str, Any]:
        """Frozen onto the draft row, so a reviewer a week later can see
        what the model was told rather than what the menu says today."""
        return {
            "item_id": self.item_id,
            "name": self.name,
            "category": self.category,
            "tags": list(self.tags),
            "cuisines": list(self.cuisines),
        }


class MenuFactsReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, model_version: str) -> None:
        self._sessions = sessions
        self._model_version = model_version

    @staticmethod
    def _owned_by(claim: str) -> Any:
        """The claim may name a BRAND or a branch (ADR-0028), so it is
        checked against both columns — the same rule order's kitchen feed
        uses. One predicate, rather than two call sites that could drift."""
        return sa.or_(item_chunks.c.restaurant_id == claim, item_chunks.c.brand_id == claim)

    async def for_items(self, *, restaurant_id: str, item_ids: Sequence[str]) -> list[MenuFacts]:
        """The facts for items that are BOTH indexed and this restaurant's.

        Returns fewer rows than asked for when an id is unknown or belongs
        to someone else, and says nothing about which — the caller reports
        the shortfall as a count, so a probe cannot distinguish "not yours"
        from "not indexed".
        """
        if not item_ids:
            return []
        return await self._read(
            sa.and_(
                self._owned_by(restaurant_id),
                item_chunks.c.item_id.in_(list(item_ids)[:MAX_ITEMS]),
            )
        )

    async def for_category(
        self, *, restaurant_id: str, category: str, limit: int = MAX_ITEMS
    ) -> list[MenuFacts]:
        """UC-25's "or a whole category". Capped, so one click cannot
        enqueue a thousand provider calls."""
        return await self._read(
            sa.and_(self._owned_by(restaurant_id), item_chunks.c.category == category),
            limit=limit,
        )

    BRANCH_FAN_OUT = 8
    """How many branch rows one dish may occupy before the cap bites.

    The SQL LIMIT applies before the dedupe below, so a brand whose dish
    exists on six branches would fill a 50-row budget with eight dishes and
    silently drop the rest. Over-fetching and capping afterwards is the
    same shape the recommendation path already uses, and bounded: a brand
    with more than eight branches loses the tail of a category rather than
    issuing an unbounded query.
    """

    async def _read(self, where: Any, *, limit: int = MAX_ITEMS) -> list[MenuFacts]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.select(
                        item_chunks.c.item_id,
                        item_chunks.c.restaurant_id,
                        item_chunks.c.brand_id,
                        item_chunks.c.name,
                        item_chunks.c.category,
                        item_chunks.c.tags,
                        item_chunks.c.cuisines,
                    )
                    .where(sa.and_(item_chunks.c.model_version == self._model_version, where))
                    .order_by(item_chunks.c.item_id)
                    .limit(limit * self.BRANCH_FAN_OUT)
                )
            ).all()
        # ONE fact per dish. A brand's branches each hold their own chunk
        # row for the same inherited item (ADR-0028), so a brand claim
        # asking about one dish matched it once per branch — four identical
        # drafts, four provider calls, for one sentence about one dish.
        # The knowledge pipeline drops the restaurant name from the
        # embedded text for exactly this reason; the same dish is the same
        # copy. `order_by(item_id)` above makes which branch's row wins
        # deterministic rather than whatever the planner returned.
        seen: set[str] = set()
        facts: list[MenuFacts] = []
        for row in rows:
            if row.item_id in seen:
                continue
            seen.add(row.item_id)
            if len(facts) >= limit:
                break
            facts.append(
                MenuFacts(
                    item_id=row.item_id,
                    restaurant_id=row.restaurant_id,
                    brand_id=row.brand_id,
                    name=row.name,
                    category=row.category,
                    tags=list(row.tags or []),
                    cuisines=list(row.cuisines or []),
                )
            )
        return facts
