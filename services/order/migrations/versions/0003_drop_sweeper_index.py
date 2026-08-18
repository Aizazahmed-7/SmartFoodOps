"""Drop the sweeper's partial index — the sweeper is gone (ADR-0023).

Placement no longer commits an order and then hopes a workflow starts:
the workflow creates the order, so a PLACED row without a saga cannot
exist and nothing scans for one. The index is left behind as dead write
cost on every insert, hence this migration.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_orders_sweeper", table_name="orders")


def downgrade() -> None:
    op.create_index(
        "ix_orders_sweeper",
        "orders",
        ["placed_at"],
        postgresql_where=sa.text("status = 'PLACED'"),
        sqlite_where=sa.text("status = 'PLACED'"),
    )
