"""Dispatch milestone: the courier lands on the order row.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("rider_id", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "rider_id")
