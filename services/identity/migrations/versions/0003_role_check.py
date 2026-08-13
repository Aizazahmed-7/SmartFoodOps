"""users.role CHECK — derived from smartfood-auth's Role vocabulary so a
role the gates can never match cannot be persisted either.

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
        "ck_users_role",
        "users",
        "role IN ('customer', 'restaurant_admin', 'rider', 'system', 'system_admin')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_role", "users")
