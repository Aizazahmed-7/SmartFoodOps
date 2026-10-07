"""Milestone timestamps: when each stage actually happened.

`updated_at` is overwritten by every move, so the row remembers only the
LAST one. An explanation engine cannot be truthful about a delay it cannot
see — "your order has been in the kitchen 40 minutes" needs the moment the
kitchen started, not the moment anything last changed (PRD-partb FR-81).

Nullable forever, and not backfilled: an order that died at VALIDATED never
had a `ready_at`, and rows that predate this migration genuinely do not know
their own history. NULL here means "not reached, or not recorded" — the
resolver treats both the same way, because both mean the same thing to a
customer asking what is happening now.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_COLUMNS = ("accepted_at", "preparing_at", "ready_at", "picked_up_at")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("orders", sa.Column(column, sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("orders", column)
