"""catalog_db initial schema: restaurants, menu structure, versions, outbox,
and the ADR-0019 search indexes (pg_trgm fuzzy + FTS expression indexes).

Revision ID: 0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restaurants",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("owner_user_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("lat", sa.Float, nullable=True),
        sa.Column("lon", sa.Float, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("hours", sa.JSON, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_restaurants_owner_user_id", "restaurants", ["owner_user_id"], unique=True
    )
    op.create_index("ix_restaurants_city", "restaurants", ["city"])

    op.create_table(
        "restaurant_cuisines",
        sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
        sa.Column("cuisine", sa.Text, primary_key=True),
    )
    op.create_index("ix_restaurant_cuisines_cuisine", "restaurant_cuisines", ["cuisine"])

    op.create_table(
        "menu_categories",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_menu_categories_restaurant_id", "menu_categories", ["restaurant_id"])

    op.create_table(
        "menu_items",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("category_id", sa.Text, sa.ForeignKey("menu_categories.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="USD"),
        sa.Column("available", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_menu_items_restaurant_id", "menu_items", ["restaurant_id"])

    op.create_table(
        "item_tags",
        sa.Column("item_id", sa.Text, sa.ForeignKey("menu_items.id"), primary_key=True),
        sa.Column("tag", sa.Text, primary_key=True),
    )
    op.create_index("ix_item_tags_tag", "item_tags", ["tag"])

    op.create_table(
        "modifier_groups",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("item_id", sa.Text, sa.ForeignKey("menu_items.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("min_select", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_select", sa.Integer, nullable=False, server_default="1"),
        sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_modifier_groups_item_id", "modifier_groups", ["item_id"])

    op.create_table(
        "modifier_options",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("group_id", sa.Text, sa.ForeignKey("modifier_groups.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("price_delta_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_modifier_options_group_id", "modifier_options", ["group_id"])

    op.create_table(
        "menu_versions",
        sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
        sa.Column("aggregate_version", sa.Integer, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_outbox_published_at", "outbox", ["published_at"])

    # ── ADR-0019 search (PG-only; unit tests never see these) ──────────
    # Locally initdb pre-creates the extension as superuser, so this no-ops;
    # on AWS the migration role needs CREATE privilege (or pre-provision it).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # Trigram GIN = typo-tolerant name matching ("biriani" → "Biryani House").
    op.execute(
        "CREATE INDEX ix_restaurants_name_trgm ON restaurants USING gin (name gin_trgm_ops)"
    )
    op.execute("CREATE INDEX ix_menu_items_name_trgm ON menu_items USING gin (name gin_trgm_ops)")
    # FTS expression indexes = ranked word matching; expressions (not stored
    # columns) so the Core metadata stays portable — the search adapter must
    # query with these exact expressions to hit the indexes.
    op.execute(
        "CREATE INDEX ix_restaurants_fts ON restaurants "
        "USING gin (to_tsvector('simple', name))"
    )
    op.execute(
        "CREATE INDEX ix_menu_items_fts ON menu_items "
        "USING gin (to_tsvector('simple', name || ' ' || coalesce(description, '')))"
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("menu_versions")
    op.drop_table("modifier_options")
    op.drop_table("modifier_groups")
    op.drop_table("item_tags")
    op.drop_table("menu_items")
    op.drop_table("menu_categories")
    op.drop_table("restaurant_cuisines")
    op.drop_table("restaurants")
