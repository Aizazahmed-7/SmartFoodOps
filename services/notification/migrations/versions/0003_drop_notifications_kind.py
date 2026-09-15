"""Drop `notifications.kind`.

Serialised on every inbox response and read by nothing: the bell renders
`title` and `body` and navigates on `order_id`. The frontend's TS type
declared the field and never touched it.

Removed now rather than later on purpose — no external client exists yet, so
this is the cheapest it will ever be. If a client later needs a machine
handle (an icon per type, grouping, a cancellations filter, or per-locale
rendering), reintroduce it as a CHECKed vocabulary rather than as free text:
`kind` was never constrained, so nothing could safely have depended on it
anyway.

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
    op.drop_column("notifications", "kind")


def downgrade() -> None:
    # Nullable on the way back: historical rows have no kind, and deriving
    # one from the stored English `title` is exactly the fragile string
    # matching a machine handle exists to avoid.
    op.add_column("notifications", sa.Column("kind", sa.Text, nullable=True))
