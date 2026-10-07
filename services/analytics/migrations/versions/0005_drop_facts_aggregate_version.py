"""Drop `order_facts.aggregate_version`.

The last of the version columns (ADR-0039). Not one of them ever appeared in
a guard: every guarded write keys on the state it actually protects —
`status = :expected`, `available >= :qty`, `active < capacity`, or a
composite PK. They were counters, read by the outbox until ADR-0038 removed
that need.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("order_facts", "aggregate_version")


def downgrade() -> None:
    op.add_column(
        "order_facts",
        sa.Column("aggregate_version", sa.Integer, nullable=False, server_default="0"),
    )
