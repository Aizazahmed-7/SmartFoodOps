"""Catalog domain — business rules and transaction ownership.

No FastAPI, no SQL. The one pattern everything here follows is the
four-write mutation (docs/service-ownership.md — Catalog): data rows +
version bump + menu_versions audit row + outbox event, one transaction.
The announcement commits WITH the change or not at all (ARCHITECTURE
invariant 1); _publish() is that rule as code.

Event payloads carry FULL aggregate state (profile + entire menu), not
diffs: catalog.changes is compacted per restaurant, so only the last event
is guaranteed to survive — it must stand alone. Event *types* stay
fine-grained for consumers that watch the live stream.

The menu tree is deliberately dicts, not dataclasses: it is a document
(the rendered-blob/event payload shape), not behavior-bearing entities.
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import CatalogRepo
from .models import Restaurant
from .ports import CachePort, GrantsPort, SearchPort

log = get_logger("catalog.service")

BROWSE_PAGE_SIZE = 20
SEARCH_PAGE_SIZE = 20

# Literal {braces} around the rid are Redis cluster hash tags — load-bearing:
# they colocate a restaurant's blob, pointer, and lock on one shard
# (service-ownership.md, placeholder notation).


def _blob_key(restaurant_id: str, version: int) -> str:
    return f"catalog:menu:{{{restaurant_id}}}:{version}"


def _ptr_key(restaurant_id: str) -> str:
    return f"catalog:menu:ptr:{{{restaurant_id}}}"


def _lock_key(restaurant_id: str) -> str:
    return f"catalog:lock:menu:{{{restaurant_id}}}"


class CatalogError(Exception):
    """Base for domain errors; the API layer maps these to envelope codes."""


class RestaurantNotFound(CatalogError):
    pass


class CategoryNotFound(CatalogError):
    pass


class CategoryNotEmpty(CatalogError):
    pass


class ItemNotFound(CatalogError):
    pass


class NothingToUpdate(CatalogError):
    pass


class StaleMenuVersion(CatalogError):
    """A version-addressed menu request for a version that is no longer
    cached and no longer current — the client must re-resolve the pointer."""


def _now() -> datetime:
    return datetime.now(UTC)


class CatalogService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        grants: GrantsPort,
        cache: CachePort,
        search: SearchPort,
    ):
        self._sessions = sessions
        self._grants = grants
        self._cache = cache
        self._search = search

    # ── reads & snapshots ──────────────────────────────────────────

    async def _read(self, repo: CatalogRepo, restaurant_id: str) -> Restaurant:
        row = await repo.get_restaurant(restaurant_id)
        if row is None:
            raise RestaurantNotFound
        cuisines = await repo.get_cuisines(restaurant_id)
        return Restaurant(
            id=row.id, name=row.name, city=row.city, cuisines=cuisines,
            status=row.status, lat=row.lat, lon=row.lon, hours=row.hours,
            version=row.version,
        )

    @staticmethod
    def _profile(restaurant: Restaurant) -> dict[str, Any]:
        return {
            "name": restaurant.name,
            "city": restaurant.city,
            "cuisines": restaurant.cuisines,
            "status": restaurant.status,
            "lat": restaurant.lat,
            "lon": restaurant.lon,
            "hours": restaurant.hours,
        }

    @staticmethod
    def _item_dict(
        item: Row[Any],
        tags: Sequence[Row[Any]],
        groups: Sequence[Row[Any]],
        options: Sequence[Row[Any]],
    ) -> dict[str, Any]:
        options_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for option in options:
            options_by_group[option.group_id].append(
                {
                    "id": option.id,
                    "name": option.name,
                    "price_delta_cents": option.price_delta_cents,
                    "rank": option.rank,
                }
            )
        return {
            "id": item.id,
            "category_id": item.category_id,
            "name": item.name,
            "description": item.description,
            "price_cents": item.price_cents,
            "currency": item.currency,
            "available": item.available,
            "rank": item.rank,
            "tags": [t.tag for t in tags],
            "modifier_groups": [
                {
                    "id": group.id,
                    "name": group.name,
                    "min_select": group.min_select,
                    "max_select": group.max_select,
                    "rank": group.rank,
                    "options": options_by_group[group.id],
                }
                for group in groups
            ],
        }

    async def _menu_snapshot(self, repo: CatalogRepo, restaurant_id: str) -> dict[str, Any]:
        """The full menu tree — also the shape the slice-5 blob renderer
        serves. Set-based reads (5 queries total regardless of menu size)."""
        categories, items, tags, groups, options = await repo.get_menu_rows(restaurant_id)
        tags_by_item: dict[str, list[Row[Any]]] = defaultdict(list)
        for tag in tags:
            tags_by_item[tag.item_id].append(tag)
        groups_by_item: dict[str, list[Row[Any]]] = defaultdict(list)
        for group in groups:
            groups_by_item[group.item_id].append(group)
        options_by_group: dict[str, list[Row[Any]]] = defaultdict(list)
        for option in options:
            options_by_group[option.group_id].append(option)

        items_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            item_groups = groups_by_item[item.id]
            item_options = [
                o for g in item_groups for o in options_by_group[g.id]
            ]
            items_by_category[item.category_id].append(
                self._item_dict(item, tags_by_item[item.id], item_groups, item_options)
            )
        return {
            "categories": [
                {
                    "id": category.id,
                    "name": category.name,
                    "rank": category.rank,
                    "items": items_by_category[category.id],
                }
                for category in categories
            ]
        }

    async def _read_item(
        self, repo: CatalogRepo, restaurant_id: str, item_id: str
    ) -> dict[str, Any]:
        rows = await repo.get_item_rows(restaurant_id, item_id)
        if rows is None:
            raise ItemNotFound
        return self._item_dict(*rows)

    # ── the four-write rule ────────────────────────────────────────

    async def _publish(
        self,
        repo: CatalogRepo,
        restaurant_id: str,
        event_type: str,
        extra: dict[str, Any] | None = None,
    ) -> Restaurant:
        """Version bump + audit row + outbox event with a full-state payload.
        Runs inside the caller's tx, so the snapshot it reads is exactly the
        state being committed. Returns the restaurant at its new version."""
        restaurant = await self._read(repo, restaurant_id)
        menu = await self._menu_snapshot(repo, restaurant_id)
        now = _now()
        version = await repo.bump_version(restaurant_id, now)
        await repo.insert_menu_version(restaurant_id, version, now)
        await repo.stage_event(
            restaurant_id=restaurant_id,
            version=version,
            event_type=event_type,
            payload={**self._profile(restaurant), "menu": menu, **(extra or {})},
            now=now,
        )
        return replace(restaurant, version=version)

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
        hours: dict[str, Any] | None,
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
                    restaurant = await self._publish(
                        repo, restaurant_id, "RestaurantCreated",
                        {"owner_user_id": owner_user_id},
                    )
                    await session.commit()
                    created = True
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
        self, restaurant_id: str, changes: dict[str, Any], cuisines: list[str] | None
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
            restaurant = await self._publish(repo, restaurant_id, "RestaurantUpdated")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))  # next read re-renders
        return restaurant

    async def set_status(self, restaurant_id: str, status: str) -> Restaurant:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            rowcount = await repo.update_restaurant(restaurant_id, {"status": status})
            if rowcount == 0:
                raise RestaurantNotFound
            event = "RestaurantPaused" if status == "paused" else "RestaurantResumed"
            restaurant = await self._publish(repo, restaurant_id, event)
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        log.info("restaurant status changed", restaurant=restaurant_id, status=status)
        return restaurant

    # ── menu: categories ───────────────────────────────────────────

    async def add_category(
        self, restaurant_id: str, *, name: str, rank: int
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            if await repo.get_restaurant(restaurant_id) is None:
                raise RestaurantNotFound
            category_id = await repo.insert_category(restaurant_id, name=name, rank=rank)
            restaurant = await self._publish(repo, restaurant_id, "CategoryAdded")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        return {"id": category_id, "name": name, "rank": rank, "version": restaurant.version}

    async def update_category(
        self, restaurant_id: str, category_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        if not changes:
            raise NothingToUpdate
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            # Ownership lives in the query: wrong restaurant → 0 rows → 404.
            if await repo.update_category(restaurant_id, category_id, changes) == 0:
                raise CategoryNotFound
            row = await repo.get_category(restaurant_id, category_id)
            assert row is not None  # just updated it inside this tx
            restaurant = await self._publish(repo, restaurant_id, "CategoryUpdated")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        return {"id": row.id, "name": row.name, "rank": row.rank,
                "version": restaurant.version}

    async def delete_category(self, restaurant_id: str, category_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            if await repo.get_category(restaurant_id, category_id) is None:
                raise CategoryNotFound
            if await repo.count_category_items(restaurant_id, category_id) > 0:
                raise CategoryNotEmpty  # explicit: move/delete items first
            await repo.delete_category(restaurant_id, category_id)
            restaurant = await self._publish(repo, restaurant_id, "CategoryDeleted")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        return {"status": "deleted", "version": restaurant.version}

    # ── menu: items ────────────────────────────────────────────────

    async def add_item(
        self,
        restaurant_id: str,
        *,
        category_id: str,
        fields: dict[str, Any],
        tags: list[str],
        modifier_groups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            # Category ownership doubles as the restaurant existence check.
            if await repo.get_category(restaurant_id, category_id) is None:
                raise CategoryNotFound
            item_id = await repo.insert_item(restaurant_id, category_id, fields)
            await repo.set_item_tags(item_id, tags)
            await repo.insert_modifier_groups(item_id, modifier_groups)
            item = await self._read_item(repo, restaurant_id, item_id)
            restaurant = await self._publish(repo, restaurant_id, "ItemAdded")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        return {**item, "version": restaurant.version}

    async def update_item(
        self,
        restaurant_id: str,
        item_id: str,
        *,
        changes: dict[str, Any],
        tags: list[str] | None,
        modifier_groups: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """`available: False` in changes is the 86 toggle. tags/modifier_groups
        are replace-the-set when given ([] clears), untouched when None."""
        if not changes and tags is None and modifier_groups is None:
            raise NothingToUpdate
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            await self._read_item(repo, restaurant_id, item_id)  # existence guard
            if "category_id" in changes:  # moving: target must be ours too
                if await repo.get_category(restaurant_id, changes["category_id"]) is None:
                    raise CategoryNotFound
            if changes:
                await repo.update_item(restaurant_id, item_id, changes)
            if tags is not None:
                await repo.set_item_tags(item_id, tags)
            if modifier_groups is not None:
                await repo.delete_item_modifiers(item_id)
                await repo.insert_modifier_groups(item_id, modifier_groups)
            item = await self._read_item(repo, restaurant_id, item_id)
            restaurant = await self._publish(repo, restaurant_id, "ItemUpdated")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        return {**item, "version": restaurant.version}

    async def delete_item(self, restaurant_id: str, item_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            # Guard BEFORE deleting children — the children of someone else's
            # item must never be touched by a cross-tenant id guess.
            if await repo.get_item(restaurant_id, item_id) is None:
                raise ItemNotFound
            await repo.delete_item_modifiers(item_id)
            await repo.delete_item_tags(item_id)
            await repo.delete_item(item_id)
            restaurant = await self._publish(repo, restaurant_id, "ItemDeleted")
            await session.commit()
        await self._cache.delete(_ptr_key(restaurant_id))
        return {"status": "deleted", "version": restaurant.version}

    # ── menu: the cached read path (docs §7 rows 3/5/6) ────────────

    async def get_menu(self, restaurant_id: str, version: int | None = None) -> dict[str, Any]:
        """Pointer → blob → render. A blob is immutable per version, so a
        version-addressed hit never touches PG; only the current version can
        be rebuilt (old-version miss → StaleMenuVersion, client re-resolves)."""
        if version is not None:
            blob = await self._cache.get(_blob_key(restaurant_id, version))
            if blob is not None:
                return json.loads(blob)
            menu = await self._render_and_cache(restaurant_id)
            if menu["version"] != version:
                raise StaleMenuVersion
            return menu

        pointer = await self._cache.get(_ptr_key(restaurant_id))
        if pointer is not None:
            # ptr TTL (7d) outlives blob TTL (24h): a dangling pointer just
            # falls through to a render, never an error.
            blob = await self._cache.get(_blob_key(restaurant_id, int(pointer)))
            if blob is not None:
                return json.loads(blob)
        return await self._render_and_cache(restaurant_id)

    async def _render_and_cache(self, restaurant_id: str) -> dict[str, Any]:
        """Singleflight render: one renderer per restaurant per 3s window;
        losers wait a beat and re-check, then render anyway — a lock must
        never fail a user. Writes blob THEN pointer: a crash between the two
        leaves an unused blob, never a pointer at nothing."""
        lock = _lock_key(restaurant_id)
        acquired = await self._cache.acquire_lock(lock, 3000)
        if not acquired:
            await asyncio.sleep(0.05)
            pointer = await self._cache.get(_ptr_key(restaurant_id))
            if pointer is not None:
                blob = await self._cache.get(_blob_key(restaurant_id, int(pointer)))
                if blob is not None:
                    return json.loads(blob)
        try:
            async with self._sessions() as session:
                restaurant, menu = await self._consistent_read(
                    CatalogRepo(session), restaurant_id
                )
            doc = {
                "restaurant_id": restaurant.id,
                "name": restaurant.name,
                "status": restaurant.status,
                "version": restaurant.version,
                **menu,
            }
            payload = json.dumps(doc)
            await self._cache.set(_blob_key(restaurant_id, restaurant.version), payload, 86400)
            await self._cache.set(_ptr_key(restaurant_id), str(restaurant.version), 604800)
            return doc
        finally:
            if acquired:
                await self._cache.release_lock(lock)

    async def _consistent_read(
        self, repo: CatalogRepo, restaurant_id: str
    ) -> tuple[Restaurant, dict[str, Any]]:
        """Version re-check: under READ COMMITTED our reads can tear (rows
        from v N+1 under version N). Caching torn content under an immutable
        version key would poison it forever — re-read the version and retry
        if it moved. Bounded: after 3 tries serve the last read (staleness
        is display-only; poisoning is not)."""
        restaurant = await self._read(repo, restaurant_id)
        menu = await self._menu_snapshot(repo, restaurant_id)
        for _ in range(2):  # bounded retries
            check = await repo.get_restaurant(restaurant_id)
            if check is None or check.version == restaurant.version:
                break
            restaurant = await self._read(repo, restaurant_id)
            menu = await self._menu_snapshot(repo, restaurant_id)
        return restaurant, menu

    # ── browse (docs §7 row 4: 60s pages, staleness is priced) ─────

    async def browse(
        self, *, city: str, cuisine: str | None, tag: str | None, page: int
    ) -> dict[str, Any]:
        key = f"catalog:browse:{city}:{cuisine or '-'}:{tag or '-'}:{page}"
        cached = await self._cache.get(key)
        if cached is not None:
            return json.loads(cached)
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            # limit+1 trick: the extra row answers has_more without a COUNT.
            rows = await repo.browse(
                city=city, cuisine=cuisine, tag=tag,
                limit=BROWSE_PAGE_SIZE + 1, offset=page * BROWSE_PAGE_SIZE,
            )
            has_more = len(rows) > BROWSE_PAGE_SIZE
            rows = rows[:BROWSE_PAGE_SIZE]
            cuisine_rows = await repo.get_cuisines_for([r.id for r in rows])
        cuisines_by_restaurant: dict[str, list[str]] = defaultdict(list)
        for row in cuisine_rows:
            cuisines_by_restaurant[row.restaurant_id].append(row.cuisine)
        doc = {
            "restaurants": [
                {
                    "id": r.id,
                    "name": r.name,
                    "city": r.city,
                    "cuisines": cuisines_by_restaurant[r.id],
                    "status": r.status,
                    "version": r.version,
                }
                for r in rows
            ],
            "page": page,
            "has_more": has_more,
        }
        await self._cache.set(key, json.dumps(doc), 60)
        return doc

    # ── the authoritative pricing read (money path) ────────────────

    async def pricing_read(self, restaurant_id: str, item_ids: list[str]) -> dict[str, Any]:
        """Computes a consistent point-in-time view for Order's pricing
        library — it persists NOTHING; the durable pricing snapshot lives in
        order_db. Deliberately bypasses every cache (money math reads truth),
        with the same bounded version re-check as the blob renderer: a price
        edit landing mid-read must not produce a mixed-version view."""
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            restaurant = await self._read(repo, restaurant_id)
            rows = await repo.get_pricing_rows(restaurant_id, item_ids)
            for _ in range(2):  # bounded retries
                check = await repo.get_restaurant(restaurant_id)
                if check is None or check.version == restaurant.version:
                    break
                restaurant = await self._read(repo, restaurant_id)
                rows = await repo.get_pricing_rows(restaurant_id, item_ids)
        items, groups, options = rows

        options_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for option in options:
            options_by_group[option.group_id].append(
                {"id": option.id, "name": option.name,
                 "price_delta_cents": option.price_delta_cents}
            )
        groups_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for group in groups:
            groups_by_item[group.item_id].append(
                {"id": group.id, "name": group.name,
                 "min_select": group.min_select, "max_select": group.max_select,
                 "options": options_by_group[group.id]}
            )
        by_id = {item.id: item for item in items}
        return {
            "restaurant": {
                "id": restaurant.id,
                "name": restaurant.name,
                "city": restaurant.city,
                "status": restaurant.status,  # paused → pricing rejects placement
                "version": restaurant.version,
            },
            "items": [
                {
                    "id": item.id,
                    "name": item.name,  # order_items snapshots the name at placement
                    "price_cents": item.price_cents,
                    "currency": item.currency,
                    "available": item.available,  # 86'd: reported, pricing decides
                    "modifier_groups": groups_by_item[item.id],
                }
                # request order, found only — deterministic for the pricing lib
                for requested in item_ids
                if (item := by_id.get(requested)) is not None
            ],
            "missing_item_ids": [i for i in item_ids if i not in by_id],
        }

    # ── search (ADR-0019: uncached — unbounded query cardinality) ──

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        cuisine: str | None,
        tag: str | None,
        page: int,
    ) -> dict[str, Any]:
        hits = await self._search.search(
            query=query, city=city, cuisine=cuisine, tag=tag,
            limit=SEARCH_PAGE_SIZE + 1, offset=page * SEARCH_PAGE_SIZE,
        )
        has_more = len(hits) > SEARCH_PAGE_SIZE
        hits = hits[:SEARCH_PAGE_SIZE]
        ids = [hit["restaurant_id"] for hit in hits]
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            rows = await repo.get_restaurants_by_ids(ids)
            cuisine_rows = await repo.get_cuisines_for(ids)
        by_id = {row.id: row for row in rows}
        cuisines_by_restaurant: dict[str, list[str]] = defaultdict(list)
        for row in cuisine_rows:
            cuisines_by_restaurant[row.restaurant_id].append(row.cuisine)
        results: list[dict[str, Any]] = []
        for hit in hits:  # hit order IS the ranking — preserve it
            row = by_id.get(hit["restaurant_id"])
            if row is None:
                continue  # vanished between index and read — drop, never 500
            results.append(
                {
                    "restaurant": {
                        "id": row.id,
                        "name": row.name,
                        "city": row.city,
                        "cuisines": cuisines_by_restaurant[row.id],
                        "status": row.status,
                        "version": row.version,
                    },
                    "score": hit["score"],
                    "matched_items": hit["matched_items"],
                }
            )
        return {"query": query, "page": page, "has_more": has_more, "results": results}
