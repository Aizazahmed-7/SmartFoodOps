"""Drop the single-role compatibility columns from users.

Migration 0005 added user_roles / riders / restaurant_owners and backfilled
them, but kept users.role / restaurant_id / rider_id so consumers could
migrate off the `role` claim one at a time. Nothing reads them now.

DEPLOY ORDER MATTERS: this lands with the release that stops emitting the
`role` claim and the X-Auth-Role header. Every service must already read
`roles` before this ships, or an un-upgraded reader gets 401s.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Highest privilege first — the same precedence smartfood_auth used to
# compute the legacy claim. Only the downgrade needs it, to collapse a role
# SET back into the one value the old column could hold.
_PRECEDENCE_SQL = """
    CASE role
        WHEN 'system' THEN 1
        WHEN 'system_admin' THEN 2
        WHEN 'restaurant_admin' THEN 3
        WHEN 'rider' THEN 4
        WHEN 'customer' THEN 5
        ELSE 6
    END
"""


def upgrade() -> None:
    # The FK to roles.name goes with the column it constrained (0004 added it).
    op.drop_constraint("fk_users_role", "users", type_="foreignkey")
    op.drop_column("users", "role")
    op.drop_column("users", "restaurant_id")
    op.drop_column("users", "rider_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("role", sa.Text, nullable=True))
    op.add_column("users", sa.Column("restaurant_id", sa.Text, nullable=True))
    op.add_column("users", sa.Column("rider_id", sa.Text, nullable=True))

    bind = op.get_bind()
    # Collapse the role set back to one value by precedence. Lossy by
    # definition: a promoted owner's `customer` role cannot be represented.
    bind.execute(
        sa.text(f"""
        UPDATE users u SET role = COALESCE((
            SELECT ur.role FROM user_roles ur
             WHERE ur.user_id = u.id
             ORDER BY {_PRECEDENCE_SQL}
             LIMIT 1
        ), 'customer')
    """)
    )
    bind.execute(
        sa.text(
            "UPDATE users u SET restaurant_id = ro.brand_id "
            "FROM restaurant_owners ro WHERE ro.user_id = u.id"
        )
    )
    # rider_id was always equal to users.id — that is why the column went.
    bind.execute(sa.text("UPDATE users u SET rider_id = u.id FROM riders r WHERE r.user_id = u.id"))

    op.alter_column("users", "role", nullable=False, server_default="customer")
    op.create_foreign_key("fk_users_role", "users", "roles", ["role"], ["name"])
