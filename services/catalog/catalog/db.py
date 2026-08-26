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

restaurants = sa.Table(
    "restaurants",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    # The user who created it — the target of the Identity restaurant_admin
    # grant. UNIQUE: one restaurant per owner (phase-1 claim model carries a
    # single restaurant_id); also the backstop against a concurrent
    # double-onboarding race.
    sa.Column("owner_user_id", sa.Text, nullable=False, unique=True, index=True),
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
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
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
