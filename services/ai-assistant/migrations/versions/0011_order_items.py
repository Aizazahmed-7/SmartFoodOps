"""Order features for the assistant (FR-75, FR-80).

One row per (order, item), projected from `OrderPlaced` by
`assistant.features.v1`. Duplicates `analytics.order_item_facts` on purpose:
analytics owns the metrics, this owns a feature the assistant answers with,
and a synchronous call between them would put a second service on the
answer path of every cold-start turn.

Facts, not counters. Popularity and taste are computed at READ time by
grouping these rows — the doctrine analytics states in its own schema, and
the reason is the same: `count = count + 1` applied twice is a lie, and no
natural key saves an increment.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("item_id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_order_items_place_time", "order_items", ["restaurant_id", "placed_at"])
    op.create_index("ix_order_items_user_time", "order_items", ["user_id", "placed_at"])


def downgrade() -> None:
    op.drop_index("ix_order_items_user_time", table_name="order_items")
    op.drop_index("ix_order_items_place_time", table_name="order_items")
    op.drop_table("order_items")
