"""Taste profiles and the browsing signal behind them (FR-75).

Built OFFLINE by a periodic task, so a recommendation is a single-row
lookup rather than an aggregation over a history — the read happens when a
customer opens the panel, before they have typed anything, which is the
least forgiving moment to spend a group-by.

`menu_views` is the second input FR-75 names. Weaker evidence, used as such:
a view says somebody looked at a RESTAURANT, not that they wanted a dish, so
it feeds restaurant familiarity and nothing else. Anonymous views are not
stored at all — a profile needs somebody to belong to.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taste_profiles",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("cuisines", sa.JSON, nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("restaurants", sa.JSON, nullable=False),
        sa.Column("ordered", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("orders", sa.Integer, nullable=False),
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "menu_views",
        sa.Column("view_id", sa.Text, primary_key=True),
        sa.Column("restaurant_id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("viewed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_menu_views_user_time", "menu_views", ["user_id", "viewed_at"])


def downgrade() -> None:
    op.drop_index("ix_menu_views_user_time", table_name="menu_views")
    op.drop_table("menu_views")
    op.drop_table("taste_profiles")
