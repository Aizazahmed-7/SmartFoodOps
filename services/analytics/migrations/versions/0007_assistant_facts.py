"""One fact row per assistant interaction (FR-94).

`c1.assistant.events` has been published through the outbox since B3 and
read by nobody — the six FR-95 metrics and FR-97's conversion attribution
both start here.

Keyed by `message_id` and carrying ABSOLUTE values, never deltas: the
outbox is at-least-once and its poller re-sends anything it published but
did not mark, so a counter would inflate every KPI each time a poller
crashed between publishing and marking.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_facts",
        sa.Column("message_id", sa.Text, primary_key=True),
        sa.Column("conversation_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("refusal_reason", sa.Text, nullable=False, server_default="none"),
        sa.Column("cache_tier", sa.Text, nullable=False, server_default=""),
        sa.Column("item_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("restaurant_ids", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("candidates", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ungrounded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Float, nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_assistant_facts_time", "assistant_facts", ["occurred_at"])
    op.create_index("ix_assistant_facts_user_time", "assistant_facts", ["user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_assistant_facts_user_time", table_name="assistant_facts")
    op.drop_index("ix_assistant_facts_time", table_name="assistant_facts")
    op.drop_table("assistant_facts")
