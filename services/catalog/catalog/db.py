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
    # city/lat/lon/status/hours/timezone/brand_id/branch_label moved to
    # branch_metadata in 0009 — see the note below that table.
    # brand | branch. The server_default is the legacy backfill: every
    # pre-brands row is a location (migration 0007 minted their brands).
    sa.Column("kind", sa.Text, nullable=False, server_default="branch"),
    sa.CheckConstraint(f"kind IN {RESTAURANT_KINDS!r}", name="ck_restaurants_kind"),
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

# ── the branch/brand column split (review 2026-09-09) ─────────────
# A brand has no address, no opening hours and no timezone, so on the single
# table those were meaningless NULLs on every brand row — and city/timezone
# were NOT NULL, which forced migration 0007 to copy the first branch's city
# onto the brand. `restaurants.city` was therefore a lie: "whichever branch
# happened to be first". Here they are unrepresentable on a brand instead.
#
# brand_id and branch_label move TOGETHER because UNIQUE(brand_id,
# branch_label) is one index and an index cannot span two tables. `kind`
# deliberately stays on `restaurants`: it is derivable (no metadata row means
# brand), but the one-brand-per-owner partial unique index below needs it as
# a column, and a partial-index predicate cannot contain a subquery.
#
# The invariant this CANNOT declare: "every branch has exactly one metadata
# row". The PK gives at-most-one; SQL has no declarative form for
# at-least-one (a CHECK cannot contain a subquery). `insert_restaurant`
# writes both rows in one transaction and is the ONLY writer — the grep-ban
# in tests/test_no_raw_restaurant_inserts.py is what keeps it the only one,
# and the read path raises rather than rendering a metadata-less branch.
# Same invariant shape, and same treatment, as identity's riders/user_roles.
# The columns branch_metadata owns — the single source for both the repo's
# write split and the domain's "a brand has no address" guard, so the two can
# never disagree about which table answers for a field.
BRANCH_OWNED_COLUMNS: tuple[str, ...] = (
    "city",
    "lat",
    "lon",
    "status",
    "hours",
    "timezone",
    "brand_id",
    "branch_label",
)

branch_metadata = sa.Table(
    "branch_metadata",
    metadata,
    sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
    sa.Column("brand_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=False, index=True),
    sa.Column("branch_label", sa.Text, nullable=False),
    sa.Column("city", sa.Text, nullable=False),
    sa.Column("lat", sa.Float, nullable=True),
    sa.Column("lon", sa.Float, nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default="open"),
    sa.CheckConstraint(f"status IN {RESTAURANT_STATUSES!r}", name="ck_branch_metadata_status"),
    sa.Column("hours", sa.JSON, nullable=True),
    # Hours are wall-clock local, so they are meaningless without the zone
    # they are read in (smartfood_pricing.is_open_at does the arithmetic).
    sa.Column("timezone", sa.Text, nullable=False, server_default="America/Chicago"),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # Moved wholesale from `restaurants`: the branch-create idempotency key.
    sa.Index("uq_branch_metadata_label", "brand_id", "branch_label", unique=True),
    # The browse filter's index moves with the column it filters.
    sa.Index("ix_branch_metadata_city", "city"),
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
