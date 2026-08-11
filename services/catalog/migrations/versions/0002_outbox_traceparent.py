"""outbox.traceparent — W3C trace context captured at staging (docs §12);
the poller lifts it into Kafka headers so async hops stay stitched.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("traceparent", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "traceparent")
