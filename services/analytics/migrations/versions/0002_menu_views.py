"""menu_views — sampled browse telemetry for the conversion funnel (S8).

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
        "menu_views",
        sa.Column("view_id", sa.Text(), primary_key=True),
        sa.Column("restaurant_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("viewed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_menu_views_restaurant_time", "menu_views", ["restaurant_id", "viewed_at"])


def downgrade() -> None:
    op.drop_index("ix_menu_views_restaurant_time", "menu_views")
    op.drop_table("menu_views")
