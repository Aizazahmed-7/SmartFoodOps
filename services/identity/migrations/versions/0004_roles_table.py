"""Roles as a seeded lookup table (ADR-0022): create roles, seed from the
Role enum, swap users.role's CHECK for an FK.

Revision ID: 0004
Revises: 0003
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from smartfood_auth import Role

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    roles = op.create_table(
        "roles",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    # Seed BEFORE the FK lands so existing users.role values resolve.
    # (Startup re-seeds idempotently — this insert covers the FK's birth.)
    now = datetime.now(UTC)
    op.bulk_insert(roles, [{"name": str(role), "created_at": now} for role in Role])
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_foreign_key("fk_users_role", "users", "roles", ["role"], ["name"])


def downgrade() -> None:
    op.drop_constraint("fk_users_role", "users", type_="foreignkey")
    op.create_check_constraint(
        "ck_users_role", "users", f"role IN {tuple(sorted(str(r) for r in Role))!r}"
    )
    op.drop_table("roles")
