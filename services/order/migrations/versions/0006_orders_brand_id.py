"""orders.brand_id — the branch's brand, for brand-scoped kitchen feeds.

Stamped from the pricing snapshot at placement (ADR-0028); legacy rows are
filled by the catalog.changes repoint consumer, so the column stays
nullable forever and the feed keeps an OR arm on restaurant_id for the
transition window (and for future branch-scoped manager claims).

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
    op.add_column("orders", sa.Column("brand_id", sa.Text(), nullable=True))
    op.create_index("ix_orders_feed_brand", "orders", ["brand_id", "status", "placed_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_feed_brand", table_name="orders")
    op.drop_column("orders", "brand_id")
