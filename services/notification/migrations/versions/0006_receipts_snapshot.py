"""Fold `receipts.items` + `receipts.totals` into one `snapshot` column.

They were written together, from one event, and read by one renderer —
they are one document: what this receipt prints. Neither is interpreted by
this service beyond rendering, so neither needed to be addressable on its
own.

Storage-only. The domain model still hands the renderer `items` and
`totals` separately (the email body needs only the latter); the unpacking
just moved into `_receipt_data`.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("receipts", sa.Column("snapshot", sa.JSON, nullable=True))
    # json_build_object, not string concatenation: it escapes the nested
    # documents correctly whatever an item name contains.
    op.get_bind().execute(
        sa.text(
            "UPDATE receipts SET snapshot = json_build_object('items', items, 'totals', totals)"
        )
    )
    op.alter_column("receipts", "snapshot", nullable=False)
    op.drop_column("receipts", "items")
    op.drop_column("receipts", "totals")


def downgrade() -> None:
    op.add_column("receipts", sa.Column("items", sa.JSON, nullable=True))
    op.add_column("receipts", sa.Column("totals", sa.JSON, nullable=True))
    op.get_bind().execute(
        sa.text("UPDATE receipts SET items = snapshot->'items', totals = snapshot->'totals'")
    )
    op.alter_column("receipts", "items", nullable=False)
    op.alter_column("receipts", "totals", nullable=False)
    op.drop_column("receipts", "snapshot")
