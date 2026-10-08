"""`VectorStore` over assistant_db (ADR-0032).

`hashes_for` is the one read that exists to save money: it says "this
restaurant already has that text", so `embed()` is skipped for every chunk
whose content did not change. Everything else here is Postgres.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import item_chunks, restaurant_chunks
from ..domain.ports import ItemUpsert, RestaurantUpsert


class PostgresVectorStore:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    async def contents_for(self, *, restaurant_id: str) -> dict[str, str]:
        stored: dict[str, str] = {}
        for table in (item_chunks, restaurant_chunks):
            rows = await self._s.execute(
                sa.select(table.c.id, table.c.content).where(
                    table.c.restaurant_id == restaurant_id,
                )
            )
            stored.update({row.id: row.content for row in rows})
        return stored

    async def retrieval_state(self, *, restaurant_id: str) -> set[tuple]:
        """Everything a RETRIEVAL predicate reads, per row.

        `content_hash` deliberately excludes `available` and `status`
        (FR-60: volatile facts are not embedded), which is right for
        deciding what to re-embed and wrong for deciding whether the cache
        is stale — retrieval hard-filters on both, and on `city`. A dish
        being 86'd or a kitchen pausing changes what a query returns while
        leaving every content hash identical, so the drain had no signal and
        cached answers kept recommending a closed kitchen (B3 review).
        """
        state: set[tuple[str, str, bool, str]] = set()
        # Only items carry `available` — a restaurant chunk is identity, not
        # a sellable thing (ADR-0032 §5's table split), so it reports True
        # and lets `status` and `city` do the work.
        items = await self._s.execute(
            sa.select(
                item_chunks.c.id,
                item_chunks.c.city,
                item_chunks.c.available,
                item_chunks.c.status,
            ).where(
                item_chunks.c.restaurant_id == restaurant_id,
            )
        )
        state.update((r.id, r.city, bool(r.available), r.status) for r in items)
        restaurants = await self._s.execute(
            sa.select(
                restaurant_chunks.c.id, restaurant_chunks.c.city, restaurant_chunks.c.status
            ).where(
                restaurant_chunks.c.restaurant_id == restaurant_id,
            )
        )
        state.update((r.id, r.city, True, r.status) for r in restaurants)
        return state

    async def cities_for(self, *, restaurant_id: str) -> set[str]:
        """Which cities this restaurant currently has rows in.

        Read BEFORE the rewrite, so a branch whose address moves from
        Springfield to Shelbyville bumps both: the rows move, and Springfield
        would otherwise keep serving cached answers recommending a
        restaurant no longer retrievable there (B3 review).
        """
        found: set[str] = set()
        for table in (item_chunks, restaurant_chunks):
            rows = await self._s.execute(
                sa.select(table.c.city).where(
                    table.c.restaurant_id == restaurant_id,
                )
            )
            found.update(row.city for row in rows)
        return found

    async def texts_for(self, *, chunk_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        """`chunk_id -> (item_id, content)` for candidates the retriever
        ranked.

        The retrieval API returns ids because catalog hydrates from its own
        tables — but the assistant's own turn has no second source: the text
        it embedded IS the text it should show a model, so it reads it here
        rather than asking catalog for a menu it would then have to re-chunk.
        """
        wanted = list(dict.fromkeys(chunk_ids))
        if not wanted:
            return {}
        rows = await self._s.execute(
            sa.select(item_chunks.c.id, item_chunks.c.item_id, item_chunks.c.content).where(
                item_chunks.c.id.in_(wanted),
            )
        )
        return {row.id: (row.item_id, row.content) for row in rows}

    async def texts_by_item(
        self,
        *,
        item_ids: Sequence[str],
        city: str | None = None,
        restaurant_id: str | None = None,
    ) -> dict[str, tuple[str, str]]:
        """`item_id -> (restaurant_id, content)`, for dishes that can be shown.

        The sibling of `texts_for`, keyed by ITEM rather than by chunk,
        because popularity and co-order signal arrive as item ids from the
        order history and have never seen a chunk id.

        **Filters what a customer may be offered**, which it did not until
        the B4 review: it matched on `model_version` and `item_id` alone, so
        the taste path carefully excluded sold-out dishes and closed
        kitchens in `menu_attributes` and then threw that away here. It could
        also resolve a dish to a branch in a DIFFERENT city, because one
        `item_id` is served by every branch of a brand (ADR-0028's fan-out).

        `restaurant_id` pins the branch when the caller already knows it — a
        co-ordered pair is only meaningful within the restaurant the orders
        happened at, and letting each half resolve independently produced
        combos spanning two branches that no cart could hold.
        """
        wanted = list(dict.fromkeys(item_ids))
        if not wanted:
            return {}
        predicates = [
            item_chunks.c.item_id.in_(wanted),
            item_chunks.c.available.is_(True),
            item_chunks.c.status == "open",
        ]
        if city is not None:
            predicates.append(item_chunks.c.city == city)
        if restaurant_id is not None:
            predicates.append(item_chunks.c.restaurant_id == restaurant_id)
        rows = await self._s.execute(
            sa.select(
                item_chunks.c.item_id, item_chunks.c.restaurant_id, item_chunks.c.content
            ).where(*predicates)
        )
        return {row.item_id: (row.restaurant_id, row.content) for row in rows}

    async def restaurants_for(self, *, item_ids: Sequence[str]) -> dict[str, str]:
        """`item_id -> restaurant_id`, for grouping cards into one snapshot
        call per restaurant.

        Read from the index rather than stored on the message, because the
        index already knows it and a second copy is a second thing that can
        disagree. An item indexed under two branches answers with one of
        them — which is correct here: the card only needs A restaurant that
        sells it, and the snapshot is what decides whether it is orderable.
        """
        wanted = list(dict.fromkeys(item_ids))
        if not wanted:
            return {}
        rows = await self._s.execute(
            sa.select(item_chunks.c.item_id, item_chunks.c.restaurant_id).where(
                item_chunks.c.item_id.in_(wanted),
            )
        )
        return {row.item_id: row.restaurant_id for row in rows}

    async def replace_restaurant(
        self,
        *,
        restaurant_id: str,
        restaurant: RestaurantUpsert,
        items: Sequence[ItemUpsert],
        now: datetime,
    ) -> int:
        await self._write_restaurant(restaurant, now)
        for item in items:
            await self._write_item(item, now)
        return await self._reconcile(restaurant_id, [i.chunk.id for i in items])

    # ── writes ──────────────────────────────────────────────────────

    async def _write_restaurant(self, upsert: RestaurantUpsert, now: datetime) -> None:
        chunk = upsert.chunk
        values = {
            "id": chunk.id,
            "restaurant_id": chunk.restaurant_id,
            "city": chunk.city,
            "brand_id": chunk.brand_id,
            "cuisines": list(chunk.cuisines),
            "status": chunk.status,
            "content": chunk.content,
            "updated_at": now,
        }
        await self._upsert(restaurant_chunks, values, upsert.embedding)

    async def _write_item(self, upsert: ItemUpsert, now: datetime) -> None:
        chunk = upsert.chunk
        values = {
            "id": chunk.id,
            "restaurant_id": chunk.restaurant_id,
            "item_id": chunk.item_id,
            "city": chunk.city,
            "brand_id": chunk.brand_id,
            "cuisines": list(chunk.cuisines),
            "category": chunk.category,
            "tags": list(chunk.tags),
            "name": chunk.name,
            "price_cents": chunk.price_cents,
            "available": chunk.available,
            "status": chunk.status,
            "content": chunk.content,
            "updated_at": now,
        }
        await self._upsert(item_chunks, values, upsert.embedding)

    async def _upsert(
        self, table: sa.Table, values: dict[str, object], embedding: Sequence[float] | None
    ) -> None:
        """`embedding is None` means the text did not change, so the stored
        vector stands and only the cheap columns move.

        That case is an UPDATE rather than an upsert, and it can be: a chunk
        whose hash matched a stored hash is a chunk that exists. Writing it
        as an upsert would force a value for a NOT NULL vector column on the
        insert path — and the only value available would be a fabricated one.
        """
        if embedding is None:
            await self._s.execute(
                sa.update(table)
                .where(table.c.id == values["id"])
                .values({k: v for k, v in values.items() if k != "id"})
            )
            return
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        stmt = insert(table).values({**values, "embedding": list(embedding)})
        await self._s.execute(
            stmt.on_conflict_do_update(
                index_elements=[table.c.id],
                set_={column: stmt.excluded[column] for column in values if column != "id"}
                | {"embedding": stmt.excluded.embedding},
            )
        )

    async def _reconcile(self, restaurant_id: str, keep: list[str]) -> int:
        """Delete every item chunk this restaurant no longer lists.

        Not housekeeping — correctness. Catalog's payloads are full
        snapshots, so a removed dish arrives as an ABSENCE and never as a
        tombstone (ADR-0033 §6). A chunk left behind is the assistant
        recommending something nobody can order.

        The restaurant chunk is exempt: there is exactly one per restaurant
        per version, and it was just upserted.
        """
        condition = sa.and_(
            item_chunks.c.restaurant_id == restaurant_id,
        )
        if keep:
            condition = sa.and_(condition, item_chunks.c.id.notin_(keep))
        result = await self._s.execute(sa.delete(item_chunks).where(condition))
        return cast("CursorResult[Any]", result).rowcount or 0
