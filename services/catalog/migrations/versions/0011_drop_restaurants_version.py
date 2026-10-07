"""Drop `restaurants.version`.

The last of the version columns (ADR-0039). Not one of them ever appeared in
a guard: every guarded write keys on the state it actually protects —
`status = :expected`, `available >= :qty`, `active < capacity`, or a
composite PK. They were counters, read by the outbox until ADR-0038 removed
that need.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("restaurants", "version")


def downgrade() -> None:
    op.add_column(
        "restaurants", sa.Column("version", sa.Integer, nullable=False, server_default="0")
    )
