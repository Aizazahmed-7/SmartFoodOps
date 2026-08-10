"""Catalog domain — business rules and transaction ownership.

No FastAPI, no SQL. The one pattern everything here follows is the
four-write mutation (docs/service-ownership.md — Catalog): data rows +
version bump + menu_versions audit row + outbox event, one transaction.
The announcement commits WITH the change or not at all (ARCHITECTURE
invariant 1); _mutate() is that rule as code.
"""

from dataclasses import replace
from datetime import UTC, datetime

from smartfood_otel import get_logger
from sqlalchemy.exc import IntegrityError

from ..adapters.repo import CatalogRepo
from .models import Restaurant
from .ports import GrantsPort

log = get_logger("catalog.service")


class CatalogError(Exception):
    """Base for domain errors; the API layer maps these to envelope codes."""


class RestaurantNotFound(CatalogError):
    pass


class NothingToUpdate(CatalogError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class CatalogService:
    def __init__(self, sessions, grants: GrantsPort):
        self._sessions = sessions
        self._grants = grants

    # ── the four-write rule ────────────────────────────────────────

    async def _publish(
        self, repo: CatalogRepo, restaurant_id: str, event_type: str, payload: dict
    ) -> int:
        """Version bump + audit row + outbox event. Callers must have
        verified the restaurant exists; runs inside the caller's tx."""
        now = _now()
        version = await repo.bump_version(restaurant_id, now)
        await repo.insert_menu_version(restaurant_id, version, now)
        await repo.stage_event(
            restaurant_id=restaurant_id,
            version=version,
            event_type=event_type,
            payload=payload,
            now=now,
        )
        return version

    @staticmethod
    def _snapshot(restaurant: Restaurant) -> dict:
        """Event payloads carry the full aggregate state, not a diff —
        catalog.changes is compacted, so each event must stand alone."""
        return {
            "name": restaurant.name,
            "city": restaurant.city,
            "cuisines": restaurant.cuisines,
            "status": restaurant.status,
            "lat": restaurant.lat,
            "lon": restaurant.lon,
            "hours": restaurant.hours,
        }

    async def _read(self, repo: CatalogRepo, restaurant_id: str) -> Restaurant:
        row = await repo.get_restaurant(restaurant_id)
        if row is None:
            raise RestaurantNotFound
        cuisines = await repo.get_cuisines(restaurant_id)
        return Restaurant(
            id=row.id,
            name=row.name,
            city=row.city,
            cuisines=cuisines,
            status=row.status,
            lat=row.lat,
            lon=row.lon,
            hours=row.hours,
            version=row.version,
        )

    # ── restaurants ────────────────────────────────────────────────

    async def create_restaurant(
        self,
        *,
        owner_user_id: str,
        name: str,
        city: str,
        cuisines: list[str],
        lat: float | None,
        lon: float | None,
        hours: dict | None,
    ) -> tuple[Restaurant, bool]:
        """Self-serve onboarding, idempotent by owner (phase-1 claim model:
        one restaurant per user, enforced by UNIQUE(owner_user_id)).

        The Identity grant runs AFTER the transaction commits — never a
        network call inside an open tx — so a failed grant leaves a committed
        restaurant, and replaying the POST lands in the repair path below.
        Returns (restaurant, created)."""
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            existing = await repo.get_restaurant_by_owner(owner_user_id)
            if existing is not None:
                restaurant, created = await self._read(repo, existing.id), False
            else:
                try:
                    restaurant_id = await repo.insert_restaurant(
                        owner_user_id=owner_user_id, name=name, city=city,
                        lat=lat, lon=lon, hours=hours, now=_now(),
                    )
                    await repo.set_cuisines(restaurant_id, cuisines)
                    fresh = await self._read(repo, restaurant_id)
                    version = await self._publish(
                        repo, restaurant_id, "RestaurantCreated",
                        {**self._snapshot(fresh), "owner_user_id": owner_user_id},
                    )
                    await session.commit()
                    restaurant, created = replace(fresh, version=version), True
                except IntegrityError:
                    # Concurrent onboarding won the UNIQUE(owner_user_id) race:
                    # roll back ours and adopt the winner.
                    await session.rollback()
                    winner = await repo.get_restaurant_by_owner(owner_user_id)
                    assert winner is not None  # the row that beat us
                    restaurant, created = await self._read(repo, winner.id), False

        # Post-commit, post-session: idempotent on Identity's side, so the
        # repair path may safely re-send it.
        await self._grants.grant_restaurant_admin(
            user_id=owner_user_id, restaurant_id=restaurant.id
        )
        log.info(
            "restaurant onboarding" if created else "restaurant onboarding replay",
            restaurant=restaurant.id,
        )
        return restaurant, created

    async def get_restaurant(self, restaurant_id: str) -> Restaurant:
        async with self._sessions() as session:
            return await self._read(CatalogRepo(session), restaurant_id)

    async def update_restaurant(
        self, restaurant_id: str, changes: dict, cuisines: list[str] | None
    ) -> Restaurant:
        if not changes and cuisines is None:
            raise NothingToUpdate
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            await self._read(repo, restaurant_id)  # existence guard before any write
            if changes:
                await repo.update_restaurant(restaurant_id, changes)
            if cuisines is not None:
                await repo.set_cuisines(restaurant_id, cuisines)
            restaurant = await self._read(repo, restaurant_id)
            version = await self._publish(
                repo, restaurant_id, "RestaurantUpdated", self._snapshot(restaurant)
            )
            await session.commit()
            return replace(restaurant, version=version)

    async def set_status(self, restaurant_id: str, status: str) -> Restaurant:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            rowcount = await repo.update_restaurant(restaurant_id, {"status": status})
            if rowcount == 0:
                raise RestaurantNotFound
            restaurant = await self._read(repo, restaurant_id)
            event = "RestaurantPaused" if status == "paused" else "RestaurantResumed"
            version = await self._publish(repo, restaurant_id, event, self._snapshot(restaurant))
            await session.commit()
            log.info("restaurant status changed", restaurant=restaurant_id, status=status)
            return replace(restaurant, version=version)
