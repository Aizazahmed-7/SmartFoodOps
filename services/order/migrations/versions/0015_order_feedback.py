"""Customer feedback on a delivered order (PRD-partb FR-91).

Part A captures no customer feedback of any kind. B6's brief asks for
feedback summaries, and summarising proxies — cancel reasons, delivery
times — would be dishonest framing: none of them is anyone saying what they
thought of the food.

PK is `order_id`, so "one row per order" is the schema rather than a rule
somebody enforces. The restaurant and brand are snapshotted from the order
at write time: a summary is scoped to one restaurant's own rows and must
not depend on a join to a table whose branch could be repointed later
(ADR-0028).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_feedback",
        sa.Column(
            "order_id",
            sa.Text,
            sa.ForeignKey("orders.order_id"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("brand_id", sa.Text, nullable=True),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_order_feedback_rating"),
    )
    # The read FR-92 makes: one restaurant's own rows, newest first.
    op.create_index(
        "ix_order_feedback_restaurant",
        "order_feedback",
        ["restaurant_id", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_feedback_restaurant", table_name="order_feedback")
    op.drop_table("order_feedback")
