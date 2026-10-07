"""Drop `orders.aggregate_version`.

The last of the version columns (ADR-0039). Not one of them ever appeared in
a guard: every guarded write keys on the state it actually protects —
`status = :expected`, `available >= :qty`, `active < capacity`, or a
composite PK. They were counters, read by the outbox until ADR-0038 removed
that need.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("orders", "aggregate_version")


def downgrade() -> None:
    op.add_column(
        "orders", sa.Column("aggregate_version", sa.Integer, nullable=False, server_default="0")
    )
