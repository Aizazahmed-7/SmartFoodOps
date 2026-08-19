"""The orders row IS placement's idempotency record (ADR-0024).

Adds orders.request_hash (the body guard a retried key is checked against)
and drops idempotency_keys: the derived order_id made the separate lock
table redundant — a retry re-derives the id and reads the row, Temporal's
USE_EXISTING referees concurrent duplicates, and the lock's whole support
system (takeover, janitor) goes with it. Payment keeps its own table via
the smartfood-idempotency library; only order's copy dies here.

request_hash is nullable: rows born before this migration have no hash and
skip the mismatch guard (their keys were cleared from clients on success
anyway).

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("request_hash", sa.Text, nullable=True))
    op.drop_table("idempotency_keys")


def downgrade() -> None:
    # The original 0001 DDL, verbatim — rows are NOT restorable (the table's
    # contents were derived state; going back just re-creates the machinery).
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
    op.drop_column("orders", "request_hash")
