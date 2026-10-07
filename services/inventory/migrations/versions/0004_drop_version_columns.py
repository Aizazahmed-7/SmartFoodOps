"""Drop the three version columns.

The last of the version columns (ADR-0039). Not one of them ever appeared in
a guard: every guarded write keys on the state it actually protects —
`status = :expected`, `available >= :qty`, `active < capacity`, or a
composite PK. They were counters, read by the outbox until ADR-0038 removed
that need.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("stock", "reservations", "restaurant_load"):
        op.drop_column(table, "version")


def downgrade() -> None:
    for table in ("stock", "reservations", "restaurant_load"):
        op.add_column(table, sa.Column("version", sa.Integer, nullable=False, server_default="0"))
