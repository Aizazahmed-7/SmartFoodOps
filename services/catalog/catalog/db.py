"""catalog_db schema — the tables from docs/service-ownership.md.

SQLAlchemy Core table objects: the single source the Alembic migration and
the test create_all both derive from. PG-only search artifacts (pg_trgm +
FTS expression indexes, ADR-0019) live in the migration, not here — this
metadata must stay sqlite-compatible for the unit suite.
"""

from typing import Literal, get_args

import sqlalchemy as sa
from smartfood_outbox import outbox_table

metadata = sa.MetaData()

# The Literal is the single source of truth (the W2 idiom, backported):
# route signatures type against it and the CHECK below is derived from it.
RestaurantStatus = Literal["open", "paused"]
RESTAURANT_STATUSES: tuple[str, ...] = get_args(RestaurantStatus)

# Brands milestone (ADR-0028): a brand row owns the base menu; branch rows
# are the physical locations customers order from. Same table on purpose —
# every menu table FKs restaurants.id, so the base menu attaches to the
# brand row with zero menu-schema changes.
RestaurantKind = Literal["brand", "branch"]
RESTAURANT_KINDS: tuple[str, ...] = get_args(RestaurantKind)

restaurants = sa.Table(
    "restaurants",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    # The user who created it — the target of the Identity restaurant_admin
    # grant. One BRAND per owner (the claim carries a single brand id), many
    # branches: the partial unique below scopes the old one-per-owner rule
    # to brand rows and stays the backstop against a concurrent
    # double-onboarding race (swapped in by migration 0007).
    sa.Column("owner_user_id", sa.Text, nullable=False, index=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("city", sa.Text, nullable=False, index=True),
    sa.Column("lat", sa.Float, nullable=True),
    sa.Column("lon", sa.Float, nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default="open"),
    sa.CheckConstraint(f"status IN {RESTAURANT_STATUSES!r}", name="ck_restaurants_status"),
    sa.Column("hours", sa.JSON, nullable=True),  # {"mon": ["11:00", "23:00"], ...}
    # Hours are wall-clock local, so they are meaningless without the zone
    # they are read in (smartfood_pricing.is_open_at does the arithmetic).
    sa.Column("timezone", sa.Text, nullable=False, server_default="America/Chicago"),
    # cache version.
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    # brand | branch. The server_default is the legacy backfill: every
    # pre-brands row is a location (migration 0007 minted their brands).
    sa.Column("kind", sa.Text, nullable=False, server_default="branch"),
    sa.CheckConstraint(f"kind IN {RESTAURANT_KINDS!r}", name="ck_restaurants_kind"),
    # A brand has no parent; a branch always has one (post-0007 invariant).
    sa.CheckConstraint(
        "(kind = 'brand' AND brand_id IS NULL) OR (kind = 'branch' AND brand_id IS NOT NULL)",
        name="ck_restaurants_kind_parent",
    ),
    sa.Column("brand_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=True, index=True),
    # Human label within the brand ("Downtown"); display_name composes
    # "{name} — {label}". Unique per brand: the branch-create idempotency key.
    sa.Column("branch_label", sa.Text, nullable=True),
    sa.Index("uq_restaurants_branch_label", "brand_id", "branch_label", unique=True),
    # One brand per owner — brand rows only, so branches (which copy the
    # owner) never collide. Both dialects the unit/live suites use honor
    # the partial predicate.
    sa.Index(
        "uq_restaurants_owner_brand",
        "owner_user_id",
        unique=True,
        postgresql_where=sa.text("kind = 'brand'"),
        sqlite_where=sa.text("kind = 'brand'"),
    ),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# Presence-only per-branch 86 of a BASE item: a row means "this branch is
# not serving this base item right now"; restore = DELETE. No columns beyond
# the pair on purpose — per-branch price overrides are a decision we made
# unrepresentable (ADR-0028). Branch-local items 86 via menu_items.available.
branch_item_overrides = sa.Table(
    "branch_item_overrides",
    metadata,
    sa.Column("branch_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
    sa.Column("item_id", sa.Text, sa.ForeignKey("menu_items.id"), primary_key=True),
)

# A restaurant has many cuisines (pakistani AND bbq): plain tag rows, not an
# ARRAY/JSON column — portable to the sqlite unit suite and exactly indexable
# for the browse filter. Values are normalized to lowercase slugs at the API
# layer so "BBQ" vs "bbq" can never fragment browsing.
restaurant_cuisines = sa.Table(
    "restaurant_cuisines",
    metadata,
    sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
    sa.Column("cuisine", sa.Text, primary_key=True),
    sa.Index("ix_restaurant_cuisines_cuisine", "cuisine"),
)

menu_categories = sa.Table(
    "menu_categories",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column(
        "restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=False, index=True
    ),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
)

menu_items = sa.Table(
    "menu_items",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    # restaurant_id is denormalized here so ownership guards and search hit one table.
    sa.Column(
        "restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=False, index=True
    ),
    sa.Column("category_id", sa.Text, sa.ForeignKey("menu_categories.id"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=True),
    sa.Column("price_cents", sa.Integer, nullable=False),  # integer cents, never floats
    sa.Column("currency", sa.Text, nullable=False, server_default="USD"),
    sa.Column("available", sa.Boolean, nullable=False, server_default=sa.true()),  # the 86 flag
    sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
)

# Filterable/searchable item attributes ("vegetarian", "halal", "spicy") —
# same tag-row pattern as restaurant_cuisines, one level down. Lowercase
# slugs, normalized at the API layer. Distinct from modifiers, which price
# customizations and are never filtered on.
item_tags = sa.Table(
    "item_tags",
    metadata,
    sa.Column("item_id", sa.Text, sa.ForeignKey("menu_items.id"), primary_key=True),
    sa.Column("tag", sa.Text, primary_key=True),
    sa.Index("ix_item_tags_tag", "tag"),
)

modifier_groups = sa.Table(
    "modifier_groups",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("item_id", sa.Text, sa.ForeignKey("menu_items.id"), nullable=False, index=True),
    sa.Column("name", sa.Text, nullable=False),  # "Size", "Add-ons"
    sa.Column("min_select", sa.Integer, nullable=False, server_default="0"),
    sa.Column("max_select", sa.Integer, nullable=False, server_default="1"),
    sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
)

modifier_options = sa.Table(
    "modifier_options",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("group_id", sa.Text, sa.ForeignKey("modifier_groups.id"), nullable=False, index=True),
    sa.Column("name", sa.Text, nullable=False),  # "Large"
    sa.Column("price_delta_cents", sa.Integer, nullable=False, server_default="0"),
    sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
)

# The 9-column contract lives with its reader (smartfood-outbox).
outbox = outbox_table(metadata)
