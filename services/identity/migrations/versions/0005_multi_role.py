"""Multi-role plus role-specific tables; refresh_tokens becomes one row per
login (review 2026-09-10, supersedes ADR-0022's single-role assumption).

Additive on `users`: role/restaurant_id/rider_id are RETAINED here so
consumers can migrate off the single-role claim; a later migration drops
them once nothing reads them.

Revision ID: 0005
Revises: 0004
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Roles that imply the holder was a customer first: `grant_restaurant_admin`
# and `grant_rider` both REQUIRE role == 'customer', so every promoted user
# provably was one — the single-role column simply overwrote the fact.
# `system` and `system_admin` are not people and get no customer role.
_PROMOTED_FROM_CUSTOMER = ("restaurant_admin", "rider")


def upgrade() -> None:
    now = datetime.now(UTC)

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.Text, sa.ForeignKey("roles.name"), primary_key=True),
        sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "riders",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("onboarded_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "restaurant_owners",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("brand_id", sa.Text, nullable=False),
        sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    bind = op.get_bind()

    # 1. Everyone keeps the role they currently hold.
    bind.execute(
        sa.text(
            "INSERT INTO user_roles (user_id, role, granted_at) SELECT id, role, :now FROM users"
        ),
        {"now": now},
    )
    # 2. Restore the `customer` role promotion destroyed. This is the whole
    #    point of the change: an owner who orders dinner is a customer, and
    #    the single column could not say so.
    bind.execute(
        sa.text(
            "INSERT INTO user_roles (user_id, role, granted_at) "
            "SELECT id, 'customer', :now FROM users WHERE role IN :promoted "
            "ON CONFLICT DO NOTHING"
        ).bindparams(sa.bindparam("promoted", expanding=True)),
        {"now": now, "promoted": list(_PROMOTED_FROM_CUSTOMER)},
    )
    # 3. Role-specific state moves to its own table. rider_id was always
    #    equal to users.id, so nothing is carried across — only the fact.
    bind.execute(
        sa.text(
            "INSERT INTO riders (user_id, onboarded_at) "
            "SELECT id, :now FROM users WHERE rider_id IS NOT NULL"
        ),
        {"now": now},
    )
    bind.execute(
        sa.text(
            "INSERT INTO restaurant_owners (user_id, brand_id, granted_at) "
            "SELECT id, restaurant_id, :now FROM users WHERE restaurant_id IS NOT NULL"
        ),
        {"now": now},
    )

    # 4. refresh_tokens: one row per token -> one row per login. Existing
    #    rows cannot be mapped faithfully (a family is many rows, and the
    #    live one is whichever has rotated_at IS NULL), and the tokens are
    #    30-day transients — so drop them and let everyone sign in again.
    #    Deliberate: one re-login beats a migration that guesses.
    bind.execute(sa.text("DELETE FROM refresh_tokens"))
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
    op.drop_column("refresh_tokens", "revoked")
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.add_column(
        "refresh_tokens",
        sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    # family_id is NOT NULL upstream; there is no value to reconstruct, so
    # the same trade applies in reverse — clear the table and re-login.
    op.get_bind().execute(sa.text("DELETE FROM refresh_tokens"))
    op.add_column("refresh_tokens", sa.Column("family_id", sa.Text, nullable=False))
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    # users.role/restaurant_id/rider_id were never dropped by this revision,
    # so the single-role columns are still populated and authoritative on
    # the way back — only the new tables need removing.
    op.drop_table("restaurant_owners")
    op.drop_table("riders")
    op.drop_table("user_roles")
