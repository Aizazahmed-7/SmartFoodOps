"""The authorization milestone: when money was actually held.

B5's explanation engine tells a cancelled order's customer either "your
card hold has been released" or "you won't be charged", and it was deriving
that from `confirmed_at`. That is wrong by one transition. `authorize_payment`
moves VALIDATED -> PAYMENT_CLEARED on success (activities.py); CONFIRMED is
a separate activity afterwards. Every order cancelled at PAYMENT_CLEARED
therefore holds real money on a real card while `confirmed_at` is NULL, and
was being told nothing was charged.

The workflow already knows — `void=stage != "PLACED"` — but that lives in
Temporal state nobody can query. This column is the same fact, on the row,
stamped by the transition that caused it.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("payment_cleared_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "payment_cleared_at")
