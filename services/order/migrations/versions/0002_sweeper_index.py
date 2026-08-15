"""Partial index for the placement sweeper's stale-PLACED scan.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_orders_sweeper",
        "orders",
        ["placed_at"],
        postgresql_where=sa.text("status = 'PLACED'"),
        sqlite_where=sa.text("status = 'PLACED'"),
    )


def downgrade() -> None:
    op.drop_index("ix_orders_sweeper", table_name="orders")
