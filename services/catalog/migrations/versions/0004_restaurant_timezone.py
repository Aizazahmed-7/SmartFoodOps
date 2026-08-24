"""restaurants.timezone — the clock a posted schedule is read against.

Hours are wall-clock LOCAL ("we open at 11") so enforcing them needs the
restaurant's own zone; UTC would open a Chicago kitchen at 6am in summer
and 5am in winter. NOT NULL with a server_default so the backfill is the
default itself — every existing row becomes the seed city's zone, which is
where every seeded restaurant actually is.

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
    op.add_column(
        "restaurants",
        sa.Column("timezone", sa.Text(), nullable=False, server_default="America/Chicago"),
    )


def downgrade() -> None:
    op.drop_column("restaurants", "timezone")
