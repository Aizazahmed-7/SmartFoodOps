"""processed_events — consumer-side dedupe for the grant-convergence
consumer (ADR-0018 / ADR-0020).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("consumer_group", sa.Text, primary_key=True),
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
