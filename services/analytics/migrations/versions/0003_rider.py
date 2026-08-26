"""Dispatch milestone: the courier lands on the fact row.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("order_facts", sa.Column("rider_id", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("order_facts", "rider_id")
