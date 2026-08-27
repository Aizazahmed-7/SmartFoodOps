"""Catalog domain — business rules and transaction ownership.

No FastAPI, no SQL. The one pattern everything here follows is the
three-write mutation (docs/service-ownership.md — Catalog): data rows +
version bump + outbox event, one transaction.
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
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from smartfood_kafka import EventType
from smartfood_otel import get_logger
from smartfood_pricing import is_open_at
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
# they colocate a restaurant's menu and its render lock on one shard
# (service-ownership.md, placeholder notation).


def _menu_key(restaurant_id: str) -> str:
    return f"catalog:menu:{{{restaurant_id}}}"


def _lock_key(restaurant_id: str) -> str:
    return f"catalog:lock:menu:{{{restaurant_id}}}"


# Cache-aside accepts one race: a reader that loaded PG just before an edit
# can SET a just-stale menu right after the edit's DELETE. Nothing corrects
# that entry, so the TTL is the ceiling on how long the lie lives — minutes,
# not the old blob's 24h. Money is never exposed: checkout prices from the
# snapshot endpoint, which bypasses every cache by design.
MENU_TTL_SECONDS = 300


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


class BranchLimitReached(CatalogError):
    """A brand may hold at most MAX_BRANCHES branches — the cap that bounds
    the base-edit fan-out transaction (ADR-0028)."""


class BrandOwnedField(CatalogError):
    """A branch PATCH tried to change a field the brand owns (name,
    cuisines) — edit the brand; copies propagate."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(field)


# Bounds the fan-out tx (a base edit stages one event per branch) — a knob
# with a reason, not a guess: ~8 short statements per branch, one commit.
MAX_BRANCHES = 20


def _now() -> datetime:
    return datetime.now(UTC)


class CatalogService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        grants: GrantsPort,
        cache: CachePort,
        search: SearchPort,
        default_timezone: str = "America/Chicago",
    ):
        self._sessions = sessions
        self._grants = grants
        self._cache = cache
        self._search = search
        self._default_timezone = default_timezone

    # ── reads & snapshots ──────────────────────────────────────────

    async def _read(self, repo: CatalogRepo, restaurant_id: str) -> Restaurant:
        row = await repo.get_restaurant(restaurant_id)
        if row is None:
            raise RestaurantNotFound
        cuisines = await repo.get_cuisines(restaurant_id)
        return Restaurant(
            id=row.id,
            owner_user_id=row.owner_user_id,
            name=row.name,
            city=row.city,
            cuisines=cuisines,
            status=row.status,
            lat=row.lat,
            lon=row.lon,
            hours=row.hours,
            timezone=row.timezone,
            version=row.version,
            kind=row.kind,
            brand_id=row.brand_id,
            branch_label=row.branch_label,
        )

    @staticmethod
    def _profile(restaurant: Restaurant) -> dict[str, Any]:
        return {
            # In every event, not just RestaurantCreated: catalog.changes is
            # COMPACTED, so any single surviving event per key must carry
            # everything any consumer needs — identity's grant convergence
            # must survive its trigger event being compacted away.
            "owner_user_id": restaurant.owner_user_id,
            "name": restaurant.name,
            "city": restaurant.city,
            "cuisines": restaurant.cuisines,
            "status": restaurant.status,
            "lat": restaurant.lat,
            "lon": restaurant.lon,
            "hours": restaurant.hours,
            "timezone": restaurant.timezone,
            # Brands (ADR-0028): brand_id is SELF for brand rows so consumers
            # can scope by payload["brand_id"] without caring about kind. A
            # transitional legacy branch (no brand minted yet) emits None —
            # never its own id, or downstream brand_id backfills would stamp
            # the branch id and their IS NULL guards would refuse the real
            # brand when the 0007 cutover event arrives.
            "kind": restaurant.kind,
            "brand_id": restaurant.id if restaurant.kind == "brand" else restaurant.brand_id,
            "branch_label": restaurant.branch_label,
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

    @staticmethod
    def _menu_scope(restaurant: Restaurant) -> list[str]:
        """Whose menu rows make up this restaurant's menu. A branch inherits
        the brand's base rows (ADR-0028); brands and legacy rows are their
        own single scope. Order matters only for determinism of the IN."""
        if restaurant.brand_id is not None:
            return [restaurant.brand_id, restaurant.id]
        return [restaurant.id]

    async def _category_in_scope(
        self, repo: CatalogRepo, restaurant: Restaurant, category_id: str
    ) -> bool:
        """A branch may file items under its own categories OR the brand's
        base categories (ADR-0028) — ownership still lives in the WHERE."""
        if await repo.get_category(restaurant.id, category_id) is not None:
            return True
        return (
            restaurant.brand_id is not None
            and await repo.get_category(restaurant.brand_id, category_id) is not None
        )

    async def _menu_snapshot(self, repo: CatalogRepo, restaurant: Restaurant) -> dict[str, Any]:
        """The EFFECTIVE menu tree: base ∪ branch-local, with branch-86'd
        base items rendered unavailable and each item stamped with its
        source ('base' = inherited, 'local' = editable here). Set-based
        reads (6 queries total regardless of menu size)."""
        scope = self._menu_scope(restaurant)
        overrides: set[str] = (
            await repo.get_override_ids(restaurant.id) if restaurant.brand_id else set()
        )
        categories, items, tags, groups, options = await repo.get_menu_rows(scope)
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
            item_options = [o for g in item_groups for o in options_by_group[g.id]]
            entry = self._item_dict(item, tags_by_item[item.id], item_groups, item_options)
            entry["source"] = "local" if item.restaurant_id == restaurant.id else "base"
            if item.id in overrides:
                entry["available"] = False  # branch-86'd base item
            items_by_category[item.category_id].append(entry)
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

    # ── the three-write rule (fanning out for brands, ADR-0028) ────

    async def _stage_one(
        self,
        repo: CatalogRepo,
        restaurant: Restaurant,
        event_type: str,
        now: datetime,
        extra: dict[str, Any] | None = None,
    ) -> Restaurant:
        """One aggregate's bump + full-state event, inside the caller's tx."""
        menu = await self._menu_snapshot(repo, restaurant)
        version = await repo.bump_version(restaurant.id, now)
        await repo.stage_event(
            restaurant_id=restaurant.id,
            version=version,
            event_type=event_type,
            payload={**self._profile(restaurant), "menu": menu, **(extra or {})},
            now=now,
        )
        return replace(restaurant, version=version)

    async def _publish(
        self,
        repo: CatalogRepo,
        restaurant_id: str,
        event_type: str,
        extra: dict[str, Any] | None = None,
    ) -> tuple[Restaurant, list[str]]:
        """Version bump + outbox event with a full-state payload, inside the
        caller's tx. For a BRANCH: one aggregate, as ever. For a BRAND, the
        fan-out (ADR-0028): every branch's version bumps and every branch
        stages its own full-EFFECTIVE-state event too — which is exactly why
        menu_version pinning, cache keying and inventory provisioning never
        had to learn about inheritance. All of it commits together with the
        caller's data writes or not at all. Returns the target at its new
        version plus every menu-cache id the caller must invalidate
        post-commit (bounded by MAX_BRANCHES)."""
        restaurant = await self._read(repo, restaurant_id)
        now = _now()
        published = await self._stage_one(repo, restaurant, event_type, now, extra)
        if restaurant.kind != "brand":
            return published, [restaurant_id]
        affected = [restaurant_id]
        for row in await repo.get_branches(restaurant_id):
            branch = await self._read(repo, row.id)
            await self._stage_one(repo, branch, event_type, now)
            affected.append(row.id)
        return published, affected

    async def _invalidate_menus(self, restaurant_ids: list[str]) -> None:
        """Post-commit cache-aside deletes — one per affected menu."""
        for restaurant_id in restaurant_ids:
            await self._cache.delete(_menu_key(restaurant_id))

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
        # Optional so the config default applies: an owner who never
        # names a zone still gets a correct schedule for the deployment.
        timezone: str | None = None,
        branch_label: str | None = None,
    ) -> tuple[Restaurant, bool]:
        """Self-serve onboarding, idempotent by owner (one BRAND per owner,
        enforced by the brand-only partial unique; ADR-0028 mints the brand
        and its first branch together).

        The Identity grant runs AFTER the transaction commits — never a
        network call inside an open tx — so a failed grant leaves a committed
        restaurant, and replaying the POST lands in the repair path below.
        Returns (restaurant, created)."""
        branch_label = branch_label or "Main"
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            existing = await repo.get_restaurant_by_owner(owner_user_id)
            if existing is not None:
                restaurant, created = await self._read(repo, existing.id), False
            else:
                try:
                    now = _now()
                    # The mint (ADR-0028): a brand carrying the base menu
                    # plus its first branch — the location customers see —
                    # in ONE transaction. The branch copies the profile;
                    # name/cuisines stay brand-owned thereafter.
                    brand_id = await repo.insert_restaurant(
                        owner_user_id=owner_user_id,
                        name=name,
                        city=city,
                        lat=lat,
                        lon=lon,
                        hours=hours,
                        timezone=timezone or self._default_timezone,
                        now=now,
                        kind="brand",
                    )
                    await repo.set_cuisines(brand_id, cuisines)
                    first_branch = await repo.insert_restaurant(
                        owner_user_id=owner_user_id,
                        name=name,
                        city=city,
                        lat=lat,
                        lon=lon,
                        hours=hours,
                        timezone=timezone or self._default_timezone,
                        now=now,
                        kind="branch",
                        brand_id=brand_id,
                        branch_label=branch_label,
                    )
                    await repo.set_cuisines(first_branch, cuisines)
                    restaurant, _ = await self._publish(
                        repo, brand_id, EventType.RESTAURANT_CREATED
                    )
                    await session.commit()
                    created = True
                except IntegrityError:
                    # Concurrent onboarding won the one-brand-per-owner race:
                    # roll back ours and adopt the winner.
                    await session.rollback()
                    winner = await repo.get_restaurant_by_owner(owner_user_id)
                    assert winner is not None  # the row that beat us
                    restaurant, created = await self._read(repo, winner.id), False

        # Post-commit, post-session: idempotent on Identity's side, so the
        # repair path may safely re-send it. The claim is the BRAND.
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
            target = await self._read(repo, restaurant_id)  # existence guard first
            if target.kind == "branch":
                # Identity is brand-owned: edit the brand, copies propagate.
                if "name" in changes:
                    raise BrandOwnedField("name")
                if cuisines is not None:
                    raise BrandOwnedField("cuisines")
            if changes:
                await repo.update_restaurant(restaurant_id, changes)
            if cuisines is not None:
                await repo.set_cuisines(restaurant_id, cuisines)
            if target.kind == "brand" and "name" in changes:
                # Branch rows carry a denormalized name copy for browse and
                # find-by-name — renamed in the same tx, announced by the
                # fan-out below.
                await repo.copy_profile_to_branches(restaurant_id, {"name": changes["name"]})
            restaurant, affected = await self._publish(
                repo, restaurant_id, EventType.RESTAURANT_UPDATED
            )
            await session.commit()
        await self._invalidate_menus(affected)  # next reads re-render
        return restaurant

    # ── branches (ADR-0028) ────────────────────────────────────────

    async def create_branch(
        self,
        brand_id: str,
        *,
        branch_label: str,
        city: str,
        lat: float | None,
        lon: float | None,
        hours: dict[str, Any] | None,
        timezone: str | None,
    ) -> tuple[Restaurant, bool]:
        """A new location under the brand. Idempotent by (brand, label) —
        the seed replays; a concurrent duplicate loses the unique race and
        adopts the winner. Returns (branch, created)."""
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            brand = await self._read(repo, brand_id)
            if brand.kind != "brand":
                raise RestaurantNotFound  # branches hang off brands only
            existing = await repo.get_branch_by_label(brand_id, branch_label)
            if existing is not None:
                return await self._read(repo, existing.id), False
            if len(await repo.get_branches(brand_id)) >= MAX_BRANCHES:
                raise BranchLimitReached
            try:
                branch_id = await repo.insert_restaurant(
                    owner_user_id=brand.owner_user_id,
                    name=brand.name,  # the denormalized copy
                    city=city,
                    lat=lat,
                    lon=lon,
                    hours=hours,
                    timezone=timezone or brand.timezone,
                    now=_now(),
                    kind="branch",
                    brand_id=brand_id,
                    branch_label=branch_label,
                )
                await repo.set_cuisines(branch_id, brand.cuisines)
                branch, affected = await self._publish(
                    repo, branch_id, EventType.RESTAURANT_CREATED
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                winner = await repo.get_branch_by_label(brand_id, branch_label)
                assert winner is not None  # the row that beat us
                return await self._read(repo, winner.id), False
        await self._invalidate_menus(affected)
        log.info("branch created", brand=brand_id, branch=branch.id, label=branch_label)
        return branch, True

    async def list_branches(self, brand_id: str) -> list[Restaurant]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            brand = await self._read(repo, brand_id)
            if brand.kind != "brand":
                raise RestaurantNotFound
            return [await self._read(repo, row.id) for row in await repo.get_branches(brand_id)]

    async def set_base_item_availability(
        self, branch_id: str, item_id: str, *, available: bool
    ) -> dict[str, Any]:
        """The per-branch 86 of a BASE item (presence-only override row).
        Touches ONLY this branch: its version bumps, its event states the
        new effective menu, its cache key dies — siblings never notice."""
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            branch = await self._read(repo, branch_id)
            if branch.brand_id is None:
                raise ItemNotFound  # brands/legacy rows hold no base items
            # Ownership in the WHERE: the item must be the brand's.
            if await repo.get_item(branch.brand_id, item_id) is None:
                raise ItemNotFound
            if available:
                await repo.delete_override(branch_id, item_id)
            else:
                await repo.upsert_override(branch_id, item_id)
            branch, affected = await self._publish(repo, branch_id, EventType.ITEM_UPDATED)
            await session.commit()
        await self._invalidate_menus(affected)
        return {"item_id": item_id, "available": available, "version": branch.version}

    async def converge_brand_events(self) -> int:
        """The cutover storm (runs at boot, after migrations): every brand
        migration 0007 minted sits at version 0 — publish it, which fans a
        fresh full-state event to every branch too. That storm is what
        drives identity's claim repoint and order/analytics brand_id heals,
        with no manual backfills anywhere. Idempotent by the version guard;
        one tx per brand, so a crash mid-storm resumes at the survivors."""
        converged = 0
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            pending = [row.id for row in await repo.get_unpublished_brands()]
        for brand_id in pending:
            async with self._sessions() as session:
                repo = CatalogRepo(session)
                _, affected = await self._publish(repo, brand_id, EventType.RESTAURANT_UPDATED)
                await session.commit()
            await self._invalidate_menus(affected)
            converged += 1
        if converged:
            log.info("brand cutover storm published", brands=converged)
        return converged

    async def set_status(self, restaurant_id: str, status: str) -> Restaurant:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            rowcount = await repo.update_restaurant(restaurant_id, {"status": status})
            if rowcount == 0:
                raise RestaurantNotFound
            event = (
                EventType.RESTAURANT_PAUSED if status == "paused" else EventType.RESTAURANT_RESUMED
            )
            restaurant, affected = await self._publish(repo, restaurant_id, event)
            await session.commit()
        await self._invalidate_menus(affected)
        log.info("restaurant status changed", restaurant=restaurant_id, status=status)
        return restaurant

    # ── menu: categories ───────────────────────────────────────────

    async def add_category(self, restaurant_id: str, *, name: str, rank: int) -> dict[str, Any]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            if await repo.get_restaurant(restaurant_id) is None:
                raise RestaurantNotFound
            category_id = await repo.insert_category(restaurant_id, name=name, rank=rank)
            restaurant, affected = await self._publish(
                repo, restaurant_id, EventType.CATEGORY_ADDED
            )
            await session.commit()
        await self._invalidate_menus(affected)
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
            restaurant, affected = await self._publish(
                repo, restaurant_id, EventType.CATEGORY_UPDATED
            )
            await session.commit()
        await self._invalidate_menus(affected)
        return {"id": row.id, "name": row.name, "rank": row.rank, "version": restaurant.version}

    async def delete_category(self, restaurant_id: str, category_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            repo = CatalogRepo(session)
            if await repo.get_category(restaurant_id, category_id) is None:
                raise CategoryNotFound
            if await repo.count_category_items(restaurant_id, category_id) > 0:
                raise CategoryNotEmpty  # explicit: move/delete items first
            await repo.delete_category(restaurant_id, category_id)
            restaurant, affected = await self._publish(
                repo, restaurant_id, EventType.CATEGORY_DELETED
            )
            await session.commit()
        await self._invalidate_menus(affected)
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
            # Category ownership doubles as the restaurant existence check;
            # a branch may also file into the brand's base categories.
            restaurant = await self._read(repo, restaurant_id)
            if not await self._category_in_scope(repo, restaurant, category_id):
                raise CategoryNotFound
            item_id = await repo.insert_item(restaurant_id, category_id, fields)
            await repo.set_item_tags(item_id, tags)
            await repo.insert_modifier_groups(item_id, modifier_groups)
            item = await self._read_item(repo, restaurant_id, item_id)
            restaurant, affected = await self._publish(repo, restaurant_id, EventType.ITEM_ADDED)
            await session.commit()
        await self._invalidate_menus(affected)
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
            if "category_id" in changes:  # moving: target must be in scope too
                mover = await self._read(repo, restaurant_id)
                if not await self._category_in_scope(repo, mover, changes["category_id"]):
                    raise CategoryNotFound
            if changes:
                await repo.update_item(restaurant_id, item_id, changes)
            if tags is not None:
                await repo.set_item_tags(item_id, tags)
            if modifier_groups is not None:
                await repo.delete_item_modifiers(item_id)
                await repo.insert_modifier_groups(item_id, modifier_groups)
            item = await self._read_item(repo, restaurant_id, item_id)
            restaurant, affected = await self._publish(repo, restaurant_id, EventType.ITEM_UPDATED)
            await session.commit()
        await self._invalidate_menus(affected)
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
            restaurant, affected = await self._publish(repo, restaurant_id, EventType.ITEM_DELETED)
            await session.commit()
        await self._invalidate_menus(affected)
        return {"status": "deleted", "version": restaurant.version}

    # ── menu: the cached read path (docs §7 rows 3/5) ──────────────

    async def get_menu(self, restaurant_id: str) -> dict[str, Any]:
        """Cache-aside: get the one mutable key; miss → render and fill.
        Every menu mutation deletes the key, and MENU_TTL_SECONDS bounds
        the delete-vs-refill race that cache-aside accepts."""
        blob = await self._cache.get(_menu_key(restaurant_id))
        if blob is not None:
            return json.loads(blob)
        return await self._render_and_cache(restaurant_id)

    async def _render_and_cache(self, restaurant_id: str) -> dict[str, Any]:
        """Singleflight render: one renderer per restaurant per 3s window;
        losers wait a beat and re-check, then render anyway — a lock must
        never fail a user."""
        lock = _lock_key(restaurant_id)
        acquired = await self._cache.acquire_lock(lock, 3000)
        if not acquired:
            # Rare enough to log at INFO; frequent lines here = lock contention.
            log.info("waiting for concurrent menu render", restaurant_id=restaurant_id)
            await asyncio.sleep(0.05)
            blob = await self._cache.get(_menu_key(restaurant_id))
            if blob is not None:
                return json.loads(blob)
        try:
            started = time.perf_counter()
            async with self._sessions() as session:
                restaurant, menu = await self._consistent_read(CatalogRepo(session), restaurant_id)
            doc = {
                "restaurant_id": restaurant.id,
                "name": restaurant.name,
                "display_name": restaurant.display_name,
                "brand_id": restaurant.brand_id,
                "status": restaurant.status,
                "version": restaurant.version,
                **menu,
            }
            await self._cache.set(_menu_key(restaurant_id), json.dumps(doc), MENU_TTL_SECONDS)
            # The miss-that-cost-something: one line per menu edit or TTL
            # expiry. Absence of these between requests = cache is serving.
            log.info(
                "menu rendered",
                restaurant_id=restaurant_id,
                version=restaurant.version,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            return doc
        finally:
            if acquired:
                await self._cache.release_lock(lock)

    async def _consistent_read(
        self, repo: CatalogRepo, restaurant_id: str
    ) -> tuple[Restaurant, dict[str, Any]]:
        """Version re-check: under READ COMMITTED our reads can tear (rows
        from v N+1 under version N). A torn doc would advertise a version
        its rows don't match — re-read the version and retry if it moved.
        Bounded: after 3 tries serve the last read (staleness is
        display-only; a mixed-version doc is not)."""
        restaurant = await self._read(repo, restaurant_id)
        menu = await self._menu_snapshot(repo, restaurant)
        for _ in range(2):  # bounded retries
            check = await repo.get_restaurant(restaurant_id)
            if check is None or check.version == restaurant.version:
                break
            restaurant = await self._read(repo, restaurant_id)
            menu = await self._menu_snapshot(repo, restaurant)
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
                city=city,
                cuisine=cuisine,
                tag=tag,
                limit=BROWSE_PAGE_SIZE + 1,
                offset=page * BROWSE_PAGE_SIZE,
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
                    # Branch identity (ADR-0028): cards title by display_name
                    # ("Biryani House — Downtown"); name stays the pure brand
                    # name so find-by-name (demo scripts) keeps working.
                    "brand_id": r.brand_id,
                    "branch_label": r.branch_label,
                    "display_name": f"{r.name} — {r.branch_label}" if r.branch_label else r.name,
                    "city": r.city,
                    "cuisines": cuisines_by_restaurant[r.id],
                    "status": r.status,
                    "version": r.version,
                    # The toy-city pins (dispatch milestone): the rider map
                    # draws every restaurant from this one browse call.
                    "lat": r.lat,
                    "lon": r.lon,
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
            scope = self._menu_scope(restaurant)
            overrides: set[str] = (
                await repo.get_override_ids(restaurant_id) if restaurant.brand_id else set()
            )
            rows = await repo.get_pricing_rows(scope, item_ids)
            for _ in range(2):  # bounded retries
                check = await repo.get_restaurant(restaurant_id)
                if check is None or check.version == restaurant.version:
                    break
                restaurant = await self._read(repo, restaurant_id)
                scope = self._menu_scope(restaurant)
                overrides = (
                    await repo.get_override_ids(restaurant_id) if restaurant.brand_id else set()
                )
                rows = await repo.get_pricing_rows(scope, item_ids)
        items, groups, options = rows

        options_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for option in options:
            options_by_group[option.group_id].append(
                {
                    "id": option.id,
                    "name": option.name,
                    "price_delta_cents": option.price_delta_cents,
                }
            )
        groups_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for group in groups:
            groups_by_item[group.item_id].append(
                {
                    "id": group.id,
                    "name": group.name,
                    "min_select": group.min_select,
                    "max_select": group.max_select,
                    "options": options_by_group[group.id],
                }
            )
        by_id = {item.id: item for item in items}
        return {
            "restaurant": {
                "id": restaurant.id,
                "name": restaurant.name,
                # What receipts/dispatch should show: "Biryani House — Downtown".
                "display_name": restaurant.display_name,
                "brand_id": restaurant.brand_id,
                "city": restaurant.city,
                "status": restaurant.status,  # paused → pricing rejects placement
                # The OTHER way to be shut, evaluated here because catalog is
                # the only party holding both the schedule and the clock. The
                # engine stays pure: it reads a boolean, not a timezone.
                "open_now": is_open_at(restaurant.hours, restaurant.timezone, _now()),
                "version": restaurant.version,
            },
            "items": [
                {
                    "id": item.id,
                    "name": item.name,  # order_items snapshots the name at placement
                    "price_cents": item.price_cents,
                    "currency": item.currency,
                    # 86'd: reported, pricing decides. A branch-86'd base
                    # item is exactly as unsellable here as a plain 86.
                    "available": item.available and item.id not in overrides,
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
            query=query,
            city=city,
            cuisine=cuisine,
            tag=tag,
            limit=SEARCH_PAGE_SIZE + 1,
            offset=page * SEARCH_PAGE_SIZE,
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
                        "brand_id": row.brand_id,
                        "branch_label": row.branch_label,
                        "display_name": (
                            f"{row.name} — {row.branch_label}" if row.branch_label else row.name
                        ),
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
