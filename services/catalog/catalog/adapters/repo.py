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
from smartfood_outbox import stage_event as stage_outbox_event
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import (
    BRANCH_OWNED_COLUMNS,
    branch_item_overrides,
    branch_metadata,
    item_tags,
    menu_categories,
    menu_items,
    modifier_groups,
    modifier_options,
    outbox,
    restaurant_cuisines,
    restaurants,
)


def _insert_ignoring_conflict(
    table: sa.Table, values: dict[str, Any], conflict_cols: list[str], dialect: str
) -> Any:
    """ON CONFLICT DO NOTHING on both suite dialects (the inventory idiom):
    PG aborts the whole tx on a constraint error, sqlite forgives it — the
    dialect-split insert removes that asymmetry."""
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    return insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict_cols)


class CatalogRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    # ── the branch/brand column split (review 2026-09-09) ──────────
    # Branch-owned values live on branch_metadata. Every restaurant read
    # goes through the two helpers below, so the join is defined ONCE.
    _BRIDGED = BRANCH_OWNED_COLUMNS

    @classmethod
    def _restaurant_cols(cls) -> list[Any]:
        """branch_metadata is the ONLY source of the branch-owned values
        (0009 dropped the copies on `restaurants`). A brand has no metadata
        row, so every one of these reads NULL for a brand — which is the
        whole point: a brand has no city, hours, timezone or status."""
        return [
            restaurants.c.id,
            restaurants.c.owner_user_id,
            restaurants.c.name,
            restaurants.c.kind,
            restaurants.c.created_at,
            restaurants.c.updated_at,
            *(branch_metadata.c[name].label(name) for name in cls._BRIDGED),
            # NULL on a kind='branch' row means the metadata row is missing —
            # the state no constraint forbids. Callers must not render it
            # (see IdentityRepo's note on riders/user_roles).
            branch_metadata.c.restaurant_id.label("metadata_id"),
        ]

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    async def begin_snapshot(self) -> None:
        """Pin every subsequent read in this transaction to ONE snapshot.

        A menu render is nine queries; under READ COMMITTED they can straddle
        a commit and build a doc whose rows came from two different menus.
        REPEATABLE READ makes that unrepresentable instead of merely unlikely
        — it replaced a bounded re-read loop that compared
        `restaurants.version` and gave up after three tries (ADR-0037).
        Read-only, so Postgres raises no serialization failures.

        Must be the FIRST thing in the transaction: the level cannot change
        once a statement has run.

        sqlite has no equivalent level and needs none — the unit suite drives
        one connection through StaticPool, so its reads cannot interleave
        with another writer in the first place.
        """
        if self._dialect == "postgresql":
            await self._s.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    @staticmethod
    def _joined(*, inner: bool) -> Any:
        """`inner=True` for reads that are branch-only anyway: the join then
        doubles as the kind filter, and a metadata-less branch disappears
        from listings instead of appearing with half its fields."""
        onclause = branch_metadata.c.restaurant_id == restaurants.c.id
        # Explicit onclause is required: branch_metadata has a SECOND FK to
        # restaurants (brand_id), so the join is otherwise ambiguous.
        return (
            restaurants.join(branch_metadata, onclause)
            if inner
            else restaurants.outerjoin(branch_metadata, onclause)
        )

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
        timezone: str,
        now: datetime,
        kind: str = "branch",
        brand_id: str | None = None,
        branch_label: str | None = None,
    ) -> str:
        restaurant_id = f"{'brd' if kind == 'brand' else 'rst'}_{uuid.uuid4().hex}"
        await self._s.execute(
            restaurants.insert().values(
                id=restaurant_id,
                owner_user_id=owner_user_id,
                name=name,
                kind=kind,
                created_at=now,
                updated_at=now,
            )
        )
        if kind == "branch":
            # Same transaction as the row above, so a branch is never visible
            # without its metadata. This is the ONLY place a branch is
            # created — the grep-ban test keeps that true (review 2026-09-09).
            await self._s.execute(
                branch_metadata.insert().values(
                    restaurant_id=restaurant_id,
                    brand_id=brand_id,
                    branch_label=branch_label,
                    city=city,
                    lat=lat,
                    lon=lon,
                    hours=hours,
                    timezone=timezone,
                    updated_at=now,
                )
            )
        return restaurant_id

    async def get_restaurant_by_owner(self, owner_user_id: str) -> Row[Any] | None:
        """The owner's BRAND — onboarding's idempotency read (a brand's
        branches copy the owner, so the kind filter keeps this one row)."""
        return (
            await self._s.execute(
                sa.select(restaurants).where(
                    (restaurants.c.owner_user_id == owner_user_id) & (restaurants.c.kind == "brand")
                )
            )
        ).one_or_none()

    async def get_branches(self, brand_id: str) -> list[Row[Any]]:
        return list(
            (
                await self._s.execute(
                    sa.select(*self._restaurant_cols())
                    .select_from(self._joined(inner=True))
                    .where(branch_metadata.c.brand_id == brand_id)
                    .order_by(branch_metadata.c.branch_label, restaurants.c.id)
                )
            ).all()
        )

    async def get_branch_by_label(self, brand_id: str, branch_label: str) -> Row[Any] | None:
        """The branch-create idempotency read. Both lookup keys moved to
        branch_metadata, which is also where uq_branch_metadata_label now
        makes the pair unique."""
        return (
            await self._s.execute(
                sa.select(*self._restaurant_cols())
                .select_from(self._joined(inner=True))
                .where(
                    (branch_metadata.c.brand_id == brand_id)
                    & (branch_metadata.c.branch_label == branch_label)
                )
            )
        ).one_or_none()

    async def copy_profile_to_branches(self, brand_id: str, changes: dict[str, Any]) -> None:
        """Brand-owned fields (name) propagate to the branches' denormalized
        copies in the SAME tx as the brand edit — browse reads branch rows.

        The membership test reads branch_metadata, not restaurants.brand_id:
        an UPDATE cannot join, so it goes through a subquery. `changes` only
        ever names restaurants-owned columns (name), so nothing is mirrored."""
        await self._s.execute(
            restaurants.update()
            .where(
                restaurants.c.id.in_(
                    sa.select(branch_metadata.c.restaurant_id).where(
                        branch_metadata.c.brand_id == brand_id
                    )
                )
            )
            .values(**changes)
        )

    async def upsert_override(self, branch_id: str, item_id: str) -> None:
        """Presence-only 86 (ADR-0028): replay-safe via conflict-ignore."""
        await self._s.execute(
            _insert_ignoring_conflict(
                branch_item_overrides,
                {"branch_id": branch_id, "item_id": item_id},
                ["branch_id", "item_id"],
                self._s.bind.dialect.name if self._s.bind is not None else "sqlite",
            )
        )

    async def delete_override(self, branch_id: str, item_id: str) -> None:
        await self._s.execute(
            branch_item_overrides.delete().where(
                (branch_item_overrides.c.branch_id == branch_id)
                & (branch_item_overrides.c.item_id == item_id)
            )
        )

    async def get_unpublished_brands(self) -> list[Row[Any]]:
        """Brands that have never staged an event — the boot backfill's
        worklist; a crash mid-storm resumes here.

        Asked of the OUTBOX rather than a version column (ADR-0037), and
        exactly equivalent: `_stage_one` bumps the version and stages the
        event in one transaction, so "never bumped" and "has no outbox row"
        were always the same set. Outbox rows are never deleted — the poller
        only stamps `published_at` — so the answer stays durable. If an
        operator ever did prune them, a converged brand would re-publish its
        storm once; every consumer of it is idempotent by design, so that is
        absorbed rather than harmful.
        """
        return list(
            (
                await self._s.execute(
                    sa.select(restaurants).where(
                        (restaurants.c.kind == "brand")
                        & ~sa.exists(
                            sa.select(sa.literal(1)).where(
                                outbox.c.aggregate_id == restaurants.c.id
                            )
                        )
                    )
                )
            ).all()
        )

    async def get_restaurant(self, restaurant_id: str) -> Row[Any] | None:
        """LEFT, not INNER: a BRAND legitimately has no metadata row, and
        this is the one read that serves both kinds. The caller checks
        `metadata_id` to tell "brand" from "branch with a missing row"."""
        return (
            await self._s.execute(
                sa.select(*self._restaurant_cols())
                .select_from(self._joined(inner=False))
                .where(restaurants.c.id == restaurant_id)
            )
        ).one_or_none()

    async def update_restaurant(
        self, restaurant_id: str, changes: dict[str, Any], now: datetime
    ) -> int:
        """Routes each change to the table that owns it. Returns the rows
        matched — 0 means the target does not exist, or (for a branch-owned
        change against a BRAND) that there is no metadata row to change."""
        owned = {k: v for k, v in changes.items() if k not in self._BRIDGED}
        branch = {k: v for k, v in changes.items() if k in self._BRIDGED}
        counts: list[int] = []
        if owned:
            result = await self._s.execute(
                restaurants.update().where(restaurants.c.id == restaurant_id).values(**owned)
            )
            counts.append(cast(CursorResult[Any], result).rowcount)
        if branch:
            # `branch_metadata.updated_at` means "this branch's metadata last
            # changed" — narrower than `restaurants.updated_at`, which
            # bump_version moves on every menu edit too.
            result = await self._s.execute(
                branch_metadata.update()
                .where(branch_metadata.c.restaurant_id == restaurant_id)
                .values(**branch, updated_at=now)
            )
            counts.append(cast(CursorResult[Any], result).rowcount)
        return min(counts) if counts else 0

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
            restaurant_cuisines.delete().where(restaurant_cuisines.c.restaurant_id == restaurant_id)
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
                    (menu_items.c.id == item_id) & (menu_items.c.restaurant_id == restaurant_id)
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

    async def update_item(self, restaurant_id: str, item_id: str, changes: dict[str, Any]) -> int:
        result = await self._s.execute(
            menu_items.update()
            .where((menu_items.c.id == item_id) & (menu_items.c.restaurant_id == restaurant_id))
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
        group_ids = sa.select(modifier_groups.c.id).where(modifier_groups.c.item_id == item_id)
        await self._s.execute(
            modifier_options.delete().where(modifier_options.c.group_id.in_(group_ids))
        )
        await self._s.execute(modifier_groups.delete().where(modifier_groups.c.item_id == item_id))

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
        # Brand rows are menu templates, not places — customers browse
        # branches only (ADR-0028). The INNER join now carries that filter
        # structurally (only branches have a metadata row); the explicit
        # kind test stays as the belt to that braces.
        query = (
            sa.select(*self._restaurant_cols())
            .select_from(self._joined(inner=True))
            .where((branch_metadata.c.city == city) & (restaurants.c.kind == "branch"))
        )
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
            # item shouldn't advertise its restaurant in a tag filter. An
            # item counts if the branch owns it OR inherits it from its
            # brand, and only if this branch hasn't locally 86'd it.
            query = query.where(
                sa.exists(
                    sa.select(sa.literal(1))
                    .select_from(menu_items.join(item_tags, item_tags.c.item_id == menu_items.c.id))
                    .where(
                        (
                            (menu_items.c.restaurant_id == restaurants.c.id)
                            | (menu_items.c.restaurant_id == branch_metadata.c.brand_id)
                        )
                        & (item_tags.c.tag == tag)
                        & (menu_items.c.available == sa.true())
                        & sa.not_(
                            sa.exists(
                                sa.select(sa.literal(1)).where(
                                    (branch_item_overrides.c.branch_id == restaurants.c.id)
                                    & (branch_item_overrides.c.item_id == menu_items.c.id)
                                )
                            )
                        )
                    )
                )
            )
        query = query.order_by(restaurants.c.name, restaurants.c.id).limit(limit).offset(offset)
        return list((await self._s.execute(query)).all())

    async def get_restaurants_by_ids(self, restaurant_ids: list[str]) -> list[Row[Any]]:
        """Search-result hydration. INNER: every search leg resolves to
        branch cards, and the caller already drops ids that come back empty
        ("vanished between index and read")."""
        if not restaurant_ids:
            return []
        return list(
            (
                await self._s.execute(
                    sa.select(*self._restaurant_cols())
                    .select_from(self._joined(inner=True))
                    .where(restaurants.c.id.in_(restaurant_ids))
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

    async def get_override_ids(self, branch_id: str) -> set[str]:
        """Base item ids this branch has locally 86'd (presence-only rows)."""
        rows = (
            await self._s.execute(
                sa.select(branch_item_overrides.c.item_id).where(
                    branch_item_overrides.c.branch_id == branch_id
                )
            )
        ).scalars()
        return set(rows)

    async def get_menu_rows(
        self, scope_ids: Sequence[str]
    ) -> tuple[
        Sequence[Row[Any]],
        Sequence[Row[Any]],
        Sequence[Row[Any]],
        Sequence[Row[Any]],
        Sequence[Row[Any]],
    ]:
        """(categories, items, tags, groups, options) for a menu scope —
        one id for brands/legacy rows, [brand_id, branch_id] for a branch's
        EFFECTIVE menu (ADR-0028). 5 queries regardless of menu size."""
        categories = (
            await self._s.execute(
                sa.select(menu_categories)
                .where(menu_categories.c.restaurant_id.in_(scope_ids))
                .order_by(menu_categories.c.rank, menu_categories.c.id)
            )
        ).all()
        items = (
            await self._s.execute(
                sa.select(menu_items)
                .where(menu_items.c.restaurant_id.in_(scope_ids))
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
        self, scope_ids: Sequence[str], item_ids: list[str]
    ) -> tuple[Sequence[Row[Any]], Sequence[Row[Any]], Sequence[Row[Any]]]:
        """(items, groups, options) for the requested ids only — the money
        path's read: ownership in the WHERE (foreign ids simply don't come
        back), scoped to the menu scope (branch + its brand, ADR-0028), no
        tags/categories (pricing doesn't render menus)."""
        items = (
            await self._s.execute(
                sa.select(menu_items).where(
                    menu_items.c.id.in_(item_ids) & menu_items.c.restaurant_id.in_(scope_ids)
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
                sa.select(item_tags).where(item_tags.c.item_id == item_id).order_by(item_tags.c.tag)
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

    async def touch(self, restaurant_id: str, now: datetime) -> None:
        """Stamp `updated_at` for the mutation that is staging an event.

        Was `bump_version`, which incremented a counter nothing guarded on:
        the version fed event identity (ADR-0035), the placement price guard
        (ADR-0036) and the torn-read re-check (ADR-0037), and all three are
        gone. What is left is the audit stamp, which is worth keeping."""
        await self._s.execute(
            restaurants.update().where(restaurants.c.id == restaurant_id).values(updated_at=now)
        )

    async def stage_event(
        self,
        *,
        restaurant_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="restaurant",
            aggregate_id=restaurant_id,
            event_type=event_type,
            payload=payload,
            now=now,
        )
