"""Make the receipt lifecycle explicit, and give the sweeper an index.

The states were inferred from three nullable columns and a row in another
table: "rendered" = `s3_key` set, "sent" = a `delivery_log` row exists,
"parked" = `failed_at` non-null. That made the sweeper's "still owed"
predicate live in `delivery_log`, so no index on `receipts` could serve it.

Measured on 300k receipts with nothing owed — the steady state, swept on a
schedule forever — the old query hash-joined both tables in full to return
zero rows: 7,395 buffers, 42 ms, growing with every order ever settled. A
partial index on `receipts` did not help and was not even used by the
planner. Against `status = 'pending'` the same sweep reads ONE page.

`delivery_log` is unchanged: it is the per-CHANNEL ledger holding the
provider's message id, which is a different fact from "this receipt is
done". `send_receipt` now writes both in one transaction.

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
    op.add_column(
        "receipts", sa.Column("status", sa.Text, nullable=False, server_default="pending")
    )
    # Derive each state from where it used to be inferred. Order matters:
    # a parked receipt may also have a delivery_log row (parked AFTER a
    # successful send is impossible today, but the column drop is
    # irreversible so the narrower predicate wins).
    op.get_bind().execute(
        sa.text("""
        UPDATE receipts r SET status = 'sent'
         WHERE EXISTS (SELECT 1 FROM delivery_log d
                        WHERE d.order_id = r.order_id AND d.channel = 'email')
    """)
    )
    op.get_bind().execute(
        sa.text("UPDATE receipts SET status = 'parked' WHERE failed_at IS NOT NULL")
    )
    op.create_check_constraint(
        "ck_receipts_status", "receipts", "status IN ('pending', 'sent', 'parked')"
    )
    op.create_index(
        "ix_receipts_owed",
        "receipts",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_column("receipts", "failed_at")


def downgrade() -> None:
    op.add_column("receipts", sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True))
    # The parking TIME is not recoverable — it was never stored anywhere
    # else. `created_at` is the closest honest stand-in, and the old
    # sweeper only ever tested this column for NULL.
    op.get_bind().execute(
        sa.text("UPDATE receipts SET failed_at = created_at WHERE status = 'parked'")
    )
    op.drop_index("ix_receipts_owed", table_name="receipts")
    op.drop_constraint("ck_receipts_status", "receipts", type_="check")
    op.drop_column("receipts", "status")
