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

# A receipt's lifecycle, made explicit (review 2026-09-15). It used to be
# inferred from three nullable columns and a row in another table:
# "rendered" = s3_key set, "sent" = a delivery_log row exists, "parked" =
# failed_at non-null. That left `s3_key` set with `rendered_at` NULL
# representable and meaningless, and — the reason this changed — made the
# sweeper's "still owed" predicate live in delivery_log, so no index on
# `receipts` could serve it. Measured on 300k receipts with nothing owed,
# the sweep hash-joined both tables in full to return zero rows; against
# the partial index below it reads ONE page.
ReceiptStatus = Literal["pending", "sent", "parked"]
RECEIPT_STATUSES: tuple[str, ...] = get_args(ReceiptStatus)

notifications = sa.Table(
    "notifications",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # ntf_<uuid5(event_id:recipient)> — replay-safe
    sa.Column("recipient_type", sa.Text, nullable=False),
    sa.Column("recipient_id", sa.Text, nullable=False),  # user_id or restaurant_id
    sa.Column("order_id", sa.Text, nullable=False),
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
    # {"items": [{name, qty, unit/line cents}], "totals": the pricing
    # snapshot verbatim} — one document because it is one thing: what this
    # receipt prints. Never interpreted here beyond rendering it, which is
    # why it stays opaque JSON rather than becoming columns.
    sa.Column("snapshot", sa.JSON, nullable=False),
    sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=False),  # event occurred_at
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),  # sweeper grace anchor
    sa.Column("s3_key", sa.Text, nullable=True),  # set by render_receipt
    sa.Column("rendered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    # pending → sent, or → parked. `parked` is the poison marker the
    # mailer's 4xx sets (retrying can never help); moving it back to
    # `pending` is the human replay lever, mirroring the DLQ story.
    sa.Column("status", sa.Text, nullable=False, server_default="pending"),
    sa.CheckConstraint(f"status IN {RECEIPT_STATUSES!r}", name="ck_receipts_status"),
)

# The sweeper's whole worklist. PARTIAL on purpose: it indexes only the
# rows that can ever be owed, so an empty sweep — the steady state, running
# forever on a schedule — touches one page instead of scanning every
# receipt the platform has ever issued.
sa.Index(
    "ix_receipts_owed",
    receipts.c.created_at,
    postgresql_where=sa.text("status = 'pending'"),
    sqlite_where=sa.text("status = 'pending'"),
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
