"""Persistence layer — the ONLY module that writes SQL against catalog_db.

A repo is a thin, stateless gateway bound to one session. It contains no
business rules and makes no transaction decisions: commit/rollback belongs
to the domain layer (same contract as identity's repo).
"""

import uuid
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from smartfood_outbox import event_id
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import menu_versions, outbox, restaurant_cuisines, restaurants


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
        hours: dict | None,
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

    async def get_restaurant_by_owner(self, owner_user_id: str) -> Row | None:
        return (
            await self._s.execute(
                sa.select(restaurants).where(restaurants.c.owner_user_id == owner_user_id)
            )
        ).one_or_none()

    async def get_restaurant(self, restaurant_id: str) -> Row | None:
        return (
            await self._s.execute(
                sa.select(restaurants).where(restaurants.c.id == restaurant_id)
            )
        ).one_or_none()

    async def update_restaurant(self, restaurant_id: str, changes: dict) -> int:
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
        payload: dict,
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
