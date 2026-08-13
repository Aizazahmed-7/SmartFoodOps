"""notification_db schema — docs/service-ownership.md (Notification row).

SQLAlchemy Core tables: the single source both the Alembic migration and the
test create_all derive from. Must stay sqlite-compatible for the unit suite.

No outbox table: this service consumes and never produces. Dedupe is
NATURAL_KEY — notification ids are deterministic per (event, recipient), so
replays collide on the PK and are conflict-ignored; no processed_events.
"""

from typing import Literal, get_args

import sqlalchemy as sa

metadata = sa.MetaData()

RecipientType = Literal["customer", "restaurant"]
RECIPIENT_TYPES: tuple[str, ...] = get_args(RecipientType)

notifications = sa.Table(
    "notifications",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # ntf_<uuid5(event_id:recipient)> — replay-safe
    sa.Column("recipient_type", sa.Text, nullable=False),
    sa.Column("recipient_id", sa.Text, nullable=False),  # user_id or restaurant_id
    sa.Column("order_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),  # event occurred_at
    sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint(f"recipient_type IN {RECIPIENT_TYPES!r}", name="ck_notifications_recipient"),
)

# The inbox read path: one keyset walk per (recipient) newest-first.
sa.Index(
    "ix_notifications_inbox",
    notifications.c.recipient_type,
    notifications.c.recipient_id,
    notifications.c.created_at.desc(),
    notifications.c.id.desc(),
)

# order_id → who to tell. Payment events carry no user_id (they are keyed
# by order), so every ORDER event upserts this projection and payment
# handling joins through it.
order_recipients = sa.Table(
    "order_recipients",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("restaurant_id", sa.Text, nullable=False),
)
