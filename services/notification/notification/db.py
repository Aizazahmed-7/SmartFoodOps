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

# ── receipts (S10, FR-41) ──────────────────────────────────────────
# The CLAIM CHECK: the OrderSettled consumer copies everything the PDF
# needs out of the full-state payload into this row, and the Celery chain
# is handed only the order_id — the broker carries a reference, tasks read
# the row, and no task ever calls another service for data. One receipt
# per order forever (PK = order_id, conflict-ignored), so event replays
# are absorbed structurally, the same way notification ids absorb them.
receipts = sa.Table(
    "receipts",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("restaurant_name", sa.Text, nullable=False),
    sa.Column("items", sa.JSON, nullable=False),  # [{name, qty, unit/line cents}]
    sa.Column("totals", sa.JSON, nullable=False),  # the pricing snapshot, verbatim
    sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=False),  # event occurred_at
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),  # sweeper grace anchor
    sa.Column("s3_key", sa.Text, nullable=True),  # set by render_receipt
    sa.Column("rendered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    # Poison marker: the mailer REJECTED this receipt (4xx — retrying can
    # never help). A non-null failed_at parks the row out of the sweeper;
    # clearing it after a fix is the replay lever, mirroring the DLQ story.
    sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
)

# Existence = sent, per channel. send_receipt checks before sending and
# records after — so a Celery retry (at-least-once by design: acks_late)
# re-sends only if the crash landed exactly between the send and the
# record, and a sweeper re-enqueue of an already-sent receipt is a no-op.
delivery_log = sa.Table(
    "delivery_log",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("channel", sa.Text, primary_key=True),  # 'email' today; SMS later
    sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("provider_message_id", sa.Text, nullable=False),
)
