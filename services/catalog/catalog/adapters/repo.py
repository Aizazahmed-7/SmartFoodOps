"""Persistence layer — the ONLY module that writes SQL against catalog_db.

A repo is a thin, stateless gateway bound to one session. It contains no
business rules and makes no transaction decisions: commit/rollback belongs
to the domain layer (same contract as identity's repo).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from smartfood_outbox import event_id
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import (
    item_tags,
    menu_categories,
    menu_items,
    menu_versions,
    modifier_groups,
    modifier_options,
    outbox,
    restaurant_cuisines,
    restaurants,
)


class CatalogRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    # ── restaurants ────────────────────────────────────────────────

    async def insert_restaurant(
        self,
        *,
        owner_user_id: str,
        name: str,
        city: str,
        lat: float | None,
        lon: float | None,
        hours: dict[str, Any] | None,
        now: datetime,
    ) -> str:
        restaurant_id = f"rst_{uuid.uuid4().hex}"
        await self._s.execute(
            restaurants.insert().values(
                id=restaurant_id,
                owner_user_id=owner_user_id,
                name=name,
                city=city,
                lat=lat,
                lon=lon,
                hours=hours,
                created_at=now,
                updated_at=now,
            )
        )
        return restaurant_id

    async def get_restaurant_by_owner(self, owner_user_id: str) -> Row[Any] | None:
        return (
            await self._s.execute(
                sa.select(restaurants).where(restaurants.c.owner_user_id == owner_user_id)
            )
        ).one_or_none()

    async def get_restaurant(self, restaurant_id: str) -> Row[Any] | None:
        return (
            await self._s.execute(
                sa.select(restaurants).where(restaurants.c.id == restaurant_id)
            )
        ).one_or_none()

    async def update_restaurant(self, restaurant_id: str, changes: dict[str, Any]) -> int:
        result = await self._s.execute(
            restaurants.update().where(restaurants.c.id == restaurant_id).values(**changes)
        )
        return cast(CursorResult[Any], result).rowcount

    # ── cuisines ───────────────────────────────────────────────────

    async def get_cuisines(self, restaurant_id: str) -> list[str]:
        rows = await self._s.execute(
            sa.select(restaurant_cuisines.c.cuisine)
            .where(restaurant_cuisines.c.restaurant_id == restaurant_id)
            .order_by(restaurant_cuisines.c.cuisine)
        )
        return list(rows.scalars())

    async def set_cuisines(self, restaurant_id: str, cuisines: list[str]) -> None:
        """Replace-the-set semantics: the given list becomes the whole truth."""
        await self._s.execute(
            restaurant_cuisines.delete().where(
                restaurant_cuisines.c.restaurant_id == restaurant_id
            )
        )
        await self._s.execute(
            restaurant_cuisines.insert(),
            [{"restaurant_id": restaurant_id, "cuisine": c} for c in cuisines],
        )

    # ── menu: categories (ownership in every WHERE) ────────────────

    async def get_category(self, restaurant_id: str, category_id: str) -> Row[Any] | None:
        return (
            await self._s.execute(
                sa.select(menu_categories).where(
                    (menu_categories.c.id == category_id)
                    & (menu_categories.c.restaurant_id == restaurant_id)
                )
            )
        ).one_or_none()

    async def insert_category(self, restaurant_id: str, *, name: str, rank: int) -> str:
        category_id = f"cat_{uuid.uuid4().hex}"
        await self._s.execute(
            menu_categories.insert().values(
                id=category_id, restaurant_id=restaurant_id, name=name, rank=rank
            )
        )
        return category_id

    async def update_category(
        self, restaurant_id: str, category_id: str, changes: dict[str, Any]
    ) -> int:
        result = await self._s.execute(
            menu_categories.update()
            .where(
                (menu_categories.c.id == category_id)
                & (menu_categories.c.restaurant_id == restaurant_id)
            )
            .values(**changes)
        )
        return cast(CursorResult[Any], result).rowcount

    async def delete_category(self, restaurant_id: str, category_id: str) -> int:
        result = await self._s.execute(
            menu_categories.delete().where(
                (menu_categories.c.id == category_id)
                & (menu_categories.c.restaurant_id == restaurant_id)
            )
        )
        return cast(CursorResult[Any], result).rowcount

    async def count_category_items(self, restaurant_id: str, category_id: str) -> int:
        return (
            await self._s.execute(
                sa.select(sa.func.count())
                .select_from(menu_items)
                .where(
                    (menu_items.c.category_id == category_id)
                    & (menu_items.c.restaurant_id == restaurant_id)
                )
            )
        ).scalar_one()

    # ── menu: items ────────────────────────────────────────────────

    async def get_item(self, restaurant_id: str, item_id: str) -> Row[Any] | None:
        return (
            await self._s.execute(
                sa.select(menu_items).where(
                    (menu_items.c.id == item_id)
                    & (menu_items.c.restaurant_id == restaurant_id)
                )
            )
        ).one_or_none()

    async def insert_item(
        self, restaurant_id: str, category_id: str, fields: dict[str, Any]
    ) -> str:
        item_id = f"itm_{uuid.uuid4().hex}"
        await self._s.execute(
            menu_items.insert().values(
                id=item_id, restaurant_id=restaurant_id, category_id=category_id, **fields
            )
        )
        return item_id

    async def update_item(
        self, restaurant_id: str, item_id: str, changes: dict[str, Any]
    ) -> int:
        result = await self._s.execute(
            menu_items.update()
            .where(
                (menu_items.c.id == item_id)
                & (menu_items.c.restaurant_id == restaurant_id)
            )
            .values(**changes)
        )
        return cast(CursorResult[Any], result).rowcount

    async def delete_item(self, item_id: str) -> None:
        # Ownership verified by the caller's get_item guard (children first).
        await self._s.execute(menu_items.delete().where(menu_items.c.id == item_id))

    async def set_item_tags(self, item_id: str, tags: list[str]) -> None:
        """Replace-the-set semantics: [] clears."""
        await self._s.execute(item_tags.delete().where(item_tags.c.item_id == item_id))
        if tags:
            await self._s.execute(
                item_tags.insert(), [{"item_id": item_id, "tag": t} for t in tags]
            )

    async def insert_modifier_groups(self, item_id: str, groups: list[dict[str, Any]]) -> None:
        """Batch inserts: one executemany for groups, one for all options."""
        if not groups:
            return
        group_rows: list[dict[str, Any]] = []
        option_rows: list[dict[str, Any]] = []
        for group in groups:
            group_id = f"mg_{uuid.uuid4().hex}"
            group_rows.append(
                {
                    "id": group_id,
                    "item_id": item_id,
                    "name": group["name"],
                    "min_select": group["min_select"],
                    "max_select": group["max_select"],
                    "rank": group["rank"],
                }
            )
            option_rows.extend(
                {
                    "id": f"mo_{uuid.uuid4().hex}",
                    "group_id": group_id,
                    "name": option["name"],
                    "price_delta_cents": option["price_delta_cents"],
                    "rank": option["rank"],
                }
                for option in group["options"]
            )
        await self._s.execute(modifier_groups.insert(), group_rows)
        await self._s.execute(modifier_options.insert(), option_rows)

    async def delete_item_modifiers(self, item_id: str) -> None:
        group_ids = sa.select(modifier_groups.c.id).where(
            modifier_groups.c.item_id == item_id
        )
        await self._s.execute(
            modifier_options.delete().where(modifier_options.c.group_id.in_(group_ids))
        )
        await self._s.execute(
            modifier_groups.delete().where(modifier_groups.c.item_id == item_id)
        )

    async def delete_item_tags(self, item_id: str) -> None:
        await self._s.execute(item_tags.delete().where(item_tags.c.item_id == item_id))

    # ── browse (set-based: 2 queries per page, never per-row) ──────

    async def browse(
        self,
        *,
        city: str,
        cuisine: str | None,
        tag: str | None,
        limit: int,
        offset: int,
    ) -> list[Row[Any]]:
        query = sa.select(restaurants).where(restaurants.c.city == city)
        if cuisine is not None:
            query = query.where(
                sa.exists(
                    sa.select(sa.literal(1)).where(
                        (restaurant_cuisines.c.restaurant_id == restaurants.c.id)
                        & (restaurant_cuisines.c.cuisine == cuisine)
                    )
                )
            )
        if tag is not None:
            # "has at least one AVAILABLE item with this tag" — an 86'd
            # item shouldn't advertise its restaurant in a tag filter.
            query = query.where(
                sa.exists(
                    sa.select(sa.literal(1))
                    .select_from(
                        menu_items.join(item_tags, item_tags.c.item_id == menu_items.c.id)
                    )
                    .where(
                        (menu_items.c.restaurant_id == restaurants.c.id)
                        & (item_tags.c.tag == tag)
                        & (menu_items.c.available == sa.true())
                    )
                )
            )
        query = query.order_by(restaurants.c.name, restaurants.c.id).limit(limit).offset(offset)
        return list((await self._s.execute(query)).all())

    async def get_restaurants_by_ids(self, restaurant_ids: list[str]) -> list[Row[Any]]:
        if not restaurant_ids:
            return []
        return list(
            (
                await self._s.execute(
                    sa.select(restaurants).where(restaurants.c.id.in_(restaurant_ids))
                )
            ).all()
        )

    async def get_cuisines_for(self, restaurant_ids: list[str]) -> list[Row[Any]]:
        if not restaurant_ids:
            return []
        return list(
            (
                await self._s.execute(
                    sa.select(restaurant_cuisines)
                    .where(restaurant_cuisines.c.restaurant_id.in_(restaurant_ids))
                    .order_by(restaurant_cuisines.c.cuisine)
                )
            ).all()
        )

    # ── menu: set-based reads (no per-row loops — repo contract) ───

    async def get_menu_rows(self, restaurant_id: str) -> tuple[
        Sequence[Row[Any]], Sequence[Row[Any]], Sequence[Row[Any]],
        Sequence[Row[Any]], Sequence[Row[Any]],
    ]:
        """(categories, items, tags, groups, options) for one restaurant —
        5 queries regardless of menu size."""
        categories = (
            await self._s.execute(
                sa.select(menu_categories)
                .where(menu_categories.c.restaurant_id == restaurant_id)
                .order_by(menu_categories.c.rank, menu_categories.c.id)
            )
        ).all()
        items = (
            await self._s.execute(
                sa.select(menu_items)
                .where(menu_items.c.restaurant_id == restaurant_id)
                .order_by(menu_items.c.rank, menu_items.c.id)
            )
        ).all()
        item_ids = [item.id for item in items]
        tags: Sequence[Row[Any]] = []
        groups: Sequence[Row[Any]] = []
        options: Sequence[Row[Any]] = []
        if item_ids:
            tags = (
                await self._s.execute(
                    sa.select(item_tags)
                    .where(item_tags.c.item_id.in_(item_ids))
                    .order_by(item_tags.c.tag)
                )
            ).all()
            groups = (
                await self._s.execute(
                    sa.select(modifier_groups)
                    .where(modifier_groups.c.item_id.in_(item_ids))
                    .order_by(modifier_groups.c.rank, modifier_groups.c.id)
                )
            ).all()
            group_ids = [group.id for group in groups]
            if group_ids:
                options = (
                    await self._s.execute(
                        sa.select(modifier_options)
                        .where(modifier_options.c.group_id.in_(group_ids))
                        .order_by(modifier_options.c.rank, modifier_options.c.id)
                    )
                ).all()
        return categories, items, tags, groups, options

    async def get_pricing_rows(
        self, restaurant_id: str, item_ids: list[str]
    ) -> tuple[Sequence[Row[Any]], Sequence[Row[Any]], Sequence[Row[Any]]]:
        """(items, groups, options) for the requested ids only — the money
        path's read: ownership in the WHERE (foreign ids simply don't come
        back), no tags/categories (pricing doesn't render menus)."""
        items = (
            await self._s.execute(
                sa.select(menu_items).where(
                    menu_items.c.id.in_(item_ids)
                    & (menu_items.c.restaurant_id == restaurant_id)
                )
            )
        ).all()
        found_ids = [item.id for item in items]
        groups: Sequence[Row[Any]] = []
        options: Sequence[Row[Any]] = []
        if found_ids:
            groups = (
                await self._s.execute(
                    sa.select(modifier_groups)
                    .where(modifier_groups.c.item_id.in_(found_ids))
                    .order_by(modifier_groups.c.rank, modifier_groups.c.id)
                )
            ).all()
            group_ids = [group.id for group in groups]
            if group_ids:
                options = (
                    await self._s.execute(
                        sa.select(modifier_options)
                        .where(modifier_options.c.group_id.in_(group_ids))
                        .order_by(modifier_options.c.rank, modifier_options.c.id)
                    )
                ).all()
        return items, groups, options

    async def get_item_rows(
        self, restaurant_id: str, item_id: str
    ) -> tuple[Row[Any], Sequence[Row[Any]], Sequence[Row[Any]], Sequence[Row[Any]]] | None:
        """(item, tags, groups, options) or None — the single-item read."""
        item = await self.get_item(restaurant_id, item_id)
        if item is None:
            return None
        tags = (
            await self._s.execute(
                sa.select(item_tags)
                .where(item_tags.c.item_id == item_id)
                .order_by(item_tags.c.tag)
            )
        ).all()
        groups = (
            await self._s.execute(
                sa.select(modifier_groups)
                .where(modifier_groups.c.item_id == item_id)
                .order_by(modifier_groups.c.rank, modifier_groups.c.id)
            )
        ).all()
        group_ids = [group.id for group in groups]
        options: Sequence[Row[Any]] = []
        if group_ids:
            options = (
                await self._s.execute(
                    sa.select(modifier_options)
                    .where(modifier_options.c.group_id.in_(group_ids))
                    .order_by(modifier_options.c.rank, modifier_options.c.id)
                )
            ).all()
        return item, tags, groups, options

    # ── the version/audit/outbox writes (_publish uses these) ──────

    async def bump_version(self, restaurant_id: str, now: datetime) -> int:
        """Callers guarantee the row exists (scalar_one raises otherwise)."""
        result = await self._s.execute(
            restaurants.update()
            .where(restaurants.c.id == restaurant_id)
            .values(version=restaurants.c.version + 1, updated_at=now)
            .returning(restaurants.c.version)
        )
        return int(result.scalar_one())

    async def insert_menu_version(
        self, restaurant_id: str, version: int, now: datetime
    ) -> None:
        await self._s.execute(
            menu_versions.insert().values(
                restaurant_id=restaurant_id, version=version, published_at=now
            )
        )

    async def stage_event(
        self,
        *,
        restaurant_id: str,
        version: int,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await self._s.execute(
            outbox.insert().values(
                id=event_id("restaurant", restaurant_id, version, event_type),
                aggregate_type="restaurant",
                aggregate_id=restaurant_id,
                aggregate_version=version,
                event_type=event_type,
                payload=payload,
                occurred_at=now,
            )
        )
