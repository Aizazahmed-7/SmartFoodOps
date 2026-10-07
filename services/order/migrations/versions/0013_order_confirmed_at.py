"""The fifth milestone: when the order reached the restaurant.

0012 added the four stages PRD-partb FR-81 names. FR-84 then asks for a
stage-based ETA whose first basis is `accept_timeout_s` — the restaurant's
decision window, which runs from CONFIRMED. Without this column the one
budget the spec names for the earliest stage cannot be applied to it, and
the resolver would have to use `placed_at` as a proxy, silently charging
the restaurant for however long the saga spent reserving stock and
authorizing a card.

Same rules as 0012: nullable, never backfilled, stamped by transition()
inside the guarded UPDATE.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "confirmed_at")
