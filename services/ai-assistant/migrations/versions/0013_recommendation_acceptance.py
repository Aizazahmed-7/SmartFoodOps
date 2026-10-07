"""What we showed, and what was ordered afterwards (FR-79).

Acceptance is a JOIN, never a client-reported boolean. A surface that grades
its own recommendations produces a number that improves whenever the client
changes, and nobody downstream can tell the difference — so the only input
is the order stream the assistant already consumes.

`recommendation_acceptances` is keyed `(shown_id, order_id)` because that is
what makes the derivation safe on an at-least-once topic: one order can
accept one showing exactly once, a redelivered `OrderPlaced` loses the
insert, and no second event is staged.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recommendations_shown",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("surface", sa.Text, nullable=False),
        sa.Column("basis", sa.Text, nullable=False, server_default=""),
        sa.Column("item_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("shown_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_shown_user_time", "recommendations_shown", ["user_id", "shown_at"])
    op.create_table(
        "recommendation_acceptances",
        sa.Column("shown_id", sa.Text, primary_key=True),
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("item_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("recommendation_acceptances")
    op.drop_index("ix_shown_user_time", table_name="recommendations_shown")
    op.drop_table("recommendations_shown")
