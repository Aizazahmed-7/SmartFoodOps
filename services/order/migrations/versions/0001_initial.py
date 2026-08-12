"""order_db initial schema: orders, order_items, idempotency_keys, outbox.

Revision ID: 0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_STATUSES = (
    "PLACED",
    "VALIDATED",
    "PAYMENT_CLEARED",
    "CONFIRMED",
    "ACCEPTED",
    "PREPARING",
    "READY",
    "PICKED_UP",
    "DELIVERED",
    "SETTLED",
    "CANCELLING",
    "CANCELLED",
    "REFUNDED",
)


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("restaurant_name_snapshot", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="PLACED"),
        sa.Column("aggregate_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("payment_method", sa.Text, nullable=False, server_default="CARD"),
        sa.Column("card_token", sa.Text, nullable=False),
        sa.Column("menu_version", sa.Integer, nullable=False),
        sa.Column("pricing_snapshot", sa.JSON, nullable=False),
        sa.Column("delivery_address_snapshot", sa.JSON, nullable=False),
        sa.Column("cancel_reason", sa.Text, nullable=True),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN {_STATUSES!r}", name="ck_orders_status"),
        sa.CheckConstraint("payment_method IN ('CARD','COD')", name="ck_orders_payment_method"),
    )
    op.create_index(
        "ix_orders_history",
        "orders",
        ["user_id", sa.text("placed_at DESC"), sa.text("order_id DESC")],
    )
    op.create_index("ix_orders_feed", "orders", ["restaurant_id", "status", "placed_at"])

    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), primary_key=True),
        sa.Column("line_no", sa.Integer, primary_key=True),
        sa.Column("menu_item_id", sa.Text, nullable=False),
        sa.Column("name_snapshot", sa.Text, nullable=False),
        sa.Column("unit_price_cents", sa.Integer, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("options_snapshot", sa.JSON, nullable=False),
        sa.Column("line_total_cents", sa.Integer, nullable=False),
        sa.CheckConstraint("qty BETWEEN 1 AND 50", name="ck_order_items_qty"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.Text, primary_key=True),
        sa.Column("idem_key", sa.Text, primary_key=True),
        sa.Column("body_hash", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="IN_PROGRESS"),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", sa.JSON, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS','COMPLETE')", name="ck_idempotency_keys_status"
        ),
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
        sa.Column("traceparent", sa.Text, nullable=True),
    )
    op.create_index("ix_outbox_published_at", "outbox", ["published_at"])


def downgrade() -> None:
    for table in ("outbox", "idempotency_keys", "order_items", "orders"):
        op.drop_table(table)
