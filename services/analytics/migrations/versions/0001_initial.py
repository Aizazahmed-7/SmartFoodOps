"""analytics_db initial schema: order_facts.

One row per order, folded from lifecycle events (see analytics/db.py for
why facts beat counters under at-least-once delivery).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_facts",
        sa.Column("order_id", sa.Text(), primary_key=True),
        sa.Column("restaurant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_order_facts_restaurant_id", "order_facts", ["restaurant_id"])
    op.create_index("ix_order_facts_placed_at", "order_facts", ["placed_at"])


def downgrade() -> None:
    op.drop_index("ix_order_facts_placed_at", "order_facts")
    op.drop_index("ix_order_facts_restaurant_id", "order_facts")
    op.drop_table("order_facts")
