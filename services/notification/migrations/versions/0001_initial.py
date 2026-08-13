"""notification_db initial schema: notifications inbox + order_recipients projection.

Revision ID: 0001
Revises:
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("recipient_type", sa.Text, nullable=False),
        sa.Column("recipient_id", sa.Text, nullable=False),
        sa.Column("order_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "recipient_type IN ('customer', 'restaurant')", name="ck_notifications_recipient"
        ),
    )
    op.create_index(
        "ix_notifications_inbox",
        "notifications",
        ["recipient_type", "recipient_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_table(
        "order_recipients",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("restaurant_id", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("order_recipients")
    op.drop_index("ix_notifications_inbox", table_name="notifications")
    op.drop_table("notifications")
