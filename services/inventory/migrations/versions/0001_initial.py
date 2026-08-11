"""inventory_db initial schema: stock, restaurant_load, reservations, outbox.

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
        "stock",
        sa.Column("item_id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("available", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("available >= 0", name="ck_stock_available_nonneg"),
    )
    op.create_index("ix_stock_restaurant_id", "stock", ["restaurant_id"])

    op.create_table(
        "restaurant_load",
        sa.Column("restaurant_id", sa.Text, primary_key=True),
        sa.Column("active", sa.Integer, nullable=False, server_default="0"),
        sa.Column("capacity", sa.Integer, nullable=False, server_default="10"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("active >= 0", name="ck_load_active_nonneg"),
    )

    op.create_table(
        "reservations",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("lines", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','released','consumed','expired')",
            name="ck_reservations_status",
        ),
    )
    op.create_index("ix_reservations_reaper", "reservations", ["status", "expires_at"])

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
        sa.Column("traceparent", sa.Text, nullable=True),
    )
    op.create_index("ix_outbox_published_at", "outbox", ["published_at"])


def downgrade() -> None:
    for table in ("outbox", "reservations", "restaurant_load", "stock"):
        op.drop_table(table)
