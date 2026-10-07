"""The index FR-92's read actually makes.

0015 indexed `(restaurant_id, submitted_at)` and called it "the read FR-92
makes". It is not: the read filters `restaurant_id = claim OR brand_id =
claim`, because a claim may name either (ADR-0028), and an OR across two
columns cannot use a single-column index. Every studio mount was a
sequential scan plus a sort of `order_feedback`, in the Order service's own
pool, on the same database as the order state machine.

With both indexes Postgres can take a BitmapOr over the two and skip the
sort for the common single-match case.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-05
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_order_feedback_brand", "order_feedback", ["brand_id", "submitted_at"])


def downgrade() -> None:
    op.drop_index("ix_order_feedback_brand", table_name="order_feedback")
