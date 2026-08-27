"""brand_id on order_facts and menu_views — brand-wide owner analytics.

Stamped from event payloads going forward (ADR-0028); legacy rows are
healed by the catalog.changes repoint consumer, so both columns stay
nullable and the owner reads scope on (brand_id OR restaurant_id).

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("order_facts", sa.Column("brand_id", sa.Text(), nullable=True))
    op.create_index("ix_order_facts_brand_id", "order_facts", ["brand_id"])
    op.add_column("menu_views", sa.Column("brand_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("menu_views", "brand_id")
    op.drop_index("ix_order_facts_brand_id", table_name="order_facts")
    op.drop_column("order_facts", "brand_id")
