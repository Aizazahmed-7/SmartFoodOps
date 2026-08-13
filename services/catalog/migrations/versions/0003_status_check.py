"""restaurants.status CHECK — the W2 vocabulary idiom backported: the
constraint is derived from the RestaurantStatus Literal, so the DB and the
type checker police the same closed set.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_restaurants_status", "restaurants", "status IN ('open', 'paused')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_restaurants_status", "restaurants")
