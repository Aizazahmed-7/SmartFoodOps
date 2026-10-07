"""Which brand owns which branch (FR-98).

A citation names a BRANCH, because that is what the assistant's index
carries; a restaurant admin's claim is normally the BRAND. Without this
mapping a brand owner sees their AI-driven conversions — which join through
`order_facts.brand_id` — and zero AI-driven views, which join through
citations.

Fed from `catalog.changes`, which is compacted and carries every branch, so
the mapping is complete without a backfill and a repoint re-scopes history
on the next event.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restaurant_brands",
        sa.Column("restaurant_id", sa.Text, primary_key=True),
        sa.Column("brand_id", sa.Text, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_restaurant_brands_brand_id", "restaurant_brands", ["brand_id"])


def downgrade() -> None:
    op.drop_index("ix_restaurant_brands_brand_id", table_name="restaurant_brands")
    op.drop_table("restaurant_brands")
