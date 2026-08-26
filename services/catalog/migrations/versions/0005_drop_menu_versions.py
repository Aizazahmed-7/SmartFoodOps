"""Drop menu_versions — a write-only audit table with no readers.

It recorded when each menu revision published, but nothing ever queried
it: dispute forensics never got built, and the versioning that matters is
carried elsewhere — restaurants.version (event ids, MenuVersionChanged at
placement, torn-read checks) and aggregate_version on every outbox event.
Removed with the cache-aside simplification wave (ADR-0027 era): keep the
version machinery, drop the unread history. If revision history is ever
wanted again, this table (or the event log, uncompacted) is the shape.

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
    op.drop_table("menu_versions")


def downgrade() -> None:
    op.create_table(
        "menu_versions",
        sa.Column("restaurant_id", sa.Text, sa.ForeignKey("restaurants.id"), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
