"""payment_db schema — docs/service-ownership.md (Payment row).

The ledger is APPEND-ONLY: no UPDATE or DELETE ever touches it (a source
grep test enforces this). Money that moved is a historical fact; mistakes
are corrected by writing a reversing pair, never by editing history —
that is the entire point of double-entry bookkeeping.
"""

from typing import Literal, get_args

import sqlalchemy as sa
from smartfood_idempotency import idempotency_table
from smartfood_outbox import outbox_table

metadata = sa.MetaData()

PaymentStatus = Literal["AUTHORIZED", "DECLINED", "CAPTURED", "VOIDED", "REFUNDED"]
PAYMENT_STATUSES: tuple[str, ...] = get_args(PaymentStatus)

# One row per order's payment: the authorization hold and its lifecycle.
payments = sa.Table(
    "payments",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("amount_cents", sa.Integer, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("card_token", sa.Text, nullable=False),
    # ADR-0018 forward-compat: a real PSP swap fills these, schema unchanged.
    sa.Column("psp", sa.Text, nullable=False, server_default="mock"),
    sa.Column("payment_intent_id", sa.Text, nullable=True),  # the PSP's ref
    sa.Column("capture_before", sa.TIMESTAMP(timezone=True), nullable=True),
    # Outbox event-id determinism: bumped on every state change.
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint(f"status IN {PAYMENT_STATUSES!r}", name="ck_payments_status"),
    sa.CheckConstraint("amount_cents >= 1", name="ck_payments_amount_positive"),
)

# Double-entry ledger: every money MOVEMENT is a balanced pair of rows
# (Σdebit == Σcredit per op_key). An authorization is a hold, not a
# movement — it never appears here; capture and refund do.
ledger = sa.Table(
    "ledger",
    metadata,
    sa.Column("entry_id", sa.Text, primary_key=True),
    sa.Column("order_id", sa.Text, nullable=False, index=True),
    sa.Column("op_key", sa.Text, nullable=False),  # "{order_id}:capture" etc.
    sa.Column("account", sa.Text, nullable=False),  # customer | platform_cash
    sa.Column("debit_cents", sa.Integer, nullable=False, server_default="0"),
    sa.Column("credit_cents", sa.Integer, nullable=False, server_default="0"),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # Exactly one side per row: a row is a debit or a credit, never both.
    sa.CheckConstraint(
        "(debit_cents > 0 AND credit_cents = 0) OR (credit_cents > 0 AND debit_cents = 0)",
        name="ck_ledger_one_side",
    ),
)

# The money idempotency table (scope "money", keys "{order_id}:{op}") —
# FR-22's read-before-execute lives in the shared lib against THIS table.
idempotency_keys = idempotency_table(metadata)

# ADR-0018 forward-compat shape: dedupes real-PSP webhooks when they exist.
# Unused in Part A — the mock PSP resolves ambiguity by key replay instead.
webhook_events = sa.Table(
    "webhook_events",
    metadata,
    sa.Column("webhook_id", sa.Text, primary_key=True),
    sa.Column("psp", sa.Text, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# The 9-column contract lives with its reader (smartfood-outbox).
outbox = outbox_table(metadata)
