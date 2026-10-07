"""Drop `outbox.aggregate_version`.

Every producer wrote it and no consumer ever compared it: per-aggregate
ordering comes from the Kafka topic key, and event identity stopped
depending on it at ADR-0035. Removed from the shared `outbox_table()`
contract in ADR-0038, so every service's outbox drops it together.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("outbox", "aggregate_version")


def downgrade() -> None:
    # 0 for historical rows: the per-aggregate sequence is not reconstructible
    # from the rows that remain, and an invented one would read as real.
    op.add_column(
        "outbox", sa.Column("aggregate_version", sa.Integer, nullable=False, server_default="0")
    )
