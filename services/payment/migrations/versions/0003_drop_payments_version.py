"""Drop `payments.version`.

The last of the version columns (ADR-0039). Not one of them ever appeared in
a guard: every guarded write keys on the state it actually protects —
`status = :expected`, `available >= :qty`, `active < capacity`, or a
composite PK. They were counters, read by the outbox until ADR-0038 removed
that need.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("payments", "version")


def downgrade() -> None:
    op.add_column("payments", sa.Column("version", sa.Integer, nullable=False, server_default="0"))
