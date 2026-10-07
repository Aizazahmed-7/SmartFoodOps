"""Item-level order facts (FR-96).

`order_facts` carries totals and no item ids at all, so recommendation
acceptance (FR-79) and taste profiles (FR-75) are unbuildable from it —
both need to know WHAT was ordered, not just how much it cost. `OrderPlaced`
has carried `items[]` since Part A, so this is a new consumer group over
existing history and no producer change: resetting `analytics.facts.items`
to the topic's start backfills every order that ever existed.

Written once and never updated. The order's lifecycle stays on
`order_facts` and is joined to — a cancelled order's items are still facts
about what was placed, and a second `status` column here would be a second
writer for something that already has one.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_item_facts",
        # The pair, not a line number: the cart splits a line per option
        # combination, so one order can carry the same dish twice. Quantities
        # are summed into one row by the projector, which is what keeps this
        # an absolute value under redelivery.
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("menu_item_id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("brand_id", sa.Text, nullable=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("name_snapshot", sa.Text, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("line_total_cents", sa.Integer, nullable=False),
        sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_item_facts_restaurant", "order_item_facts", ["restaurant_id"])
    # Two access shapes, two indexes: popularity (FR-80) reads by item over a
    # window, taste profiles (FR-75) read by user.
    op.create_index("ix_item_facts_item_time", "order_item_facts", ["menu_item_id", "placed_at"])
    op.create_index("ix_item_facts_user_time", "order_item_facts", ["user_id", "placed_at"])


def downgrade() -> None:
    op.drop_index("ix_item_facts_user_time", table_name="order_item_facts")
    op.drop_index("ix_item_facts_item_time", table_name="order_item_facts")
    op.drop_index("ix_item_facts_restaurant", table_name="order_item_facts")
    op.drop_table("order_item_facts")
