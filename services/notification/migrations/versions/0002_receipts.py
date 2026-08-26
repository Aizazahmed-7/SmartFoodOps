"""Receipts pipeline (S10, FR-41): claim-check rows + delivery log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receipts",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("restaurant_name", sa.Text, nullable=False),
        sa.Column("items", sa.JSON, nullable=False),
        sa.Column("totals", sa.JSON, nullable=False),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("s3_key", sa.Text, nullable=True),
        sa.Column("rendered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_table(
        "delivery_log",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("channel", sa.Text, primary_key=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("provider_message_id", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("delivery_log")
    op.drop_table("receipts")
