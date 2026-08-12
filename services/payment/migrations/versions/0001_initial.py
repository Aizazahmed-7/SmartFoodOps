"""payment_db initial schema: payments, ledger, idempotency_keys,
webhook_events, outbox.

Revision ID: 0001
Revises: None
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("card_token", sa.Text, nullable=False),
        sa.Column("psp", sa.Text, nullable=False, server_default="mock"),
        sa.Column("payment_intent_id", sa.Text, nullable=True),
        sa.Column("capture_before", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('AUTHORIZED','DECLINED','CAPTURED','VOIDED','REFUNDED')",
            name="ck_payments_status",
        ),
        sa.CheckConstraint("amount_cents >= 1", name="ck_payments_amount_positive"),
    )

    op.create_table(
        "ledger",
        sa.Column("entry_id", sa.Text, primary_key=True),
        sa.Column("order_id", sa.Text, nullable=False),
        sa.Column("op_key", sa.Text, nullable=False),
        sa.Column("account", sa.Text, nullable=False),
        sa.Column("debit_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("credit_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("currency", sa.Text, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(debit_cents > 0 AND credit_cents = 0) OR (credit_cents > 0 AND debit_cents = 0)",
            name="ck_ledger_one_side",
        ),
    )
    op.create_index("ix_ledger_order_id", "ledger", ["order_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.Text, primary_key=True),
        sa.Column("idem_key", sa.Text, primary_key=True),
        sa.Column("body_hash", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="IN_PROGRESS"),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", sa.JSON, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS','COMPLETE')", name="ck_idempotency_keys_status"
        ),
    )

    op.create_table(
        "webhook_events",
        sa.Column("webhook_id", sa.Text, primary_key=True),
        sa.Column("psp", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("aggregate_type", sa.Text, nullable=False),
        sa.Column("aggregate_id", sa.Text, nullable=False),
        sa.Column("aggregate_version", sa.Integer, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("traceparent", sa.Text, nullable=True),
    )
    op.create_index("ix_outbox_published_at", "outbox", ["published_at"])


def downgrade() -> None:
    for table in ("outbox", "webhook_events", "idempotency_keys", "ledger", "payments"):
        op.drop_table(table)
