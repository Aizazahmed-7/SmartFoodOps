"""Drop `order_recipients`.

It existed so refund notifications could find the customer: payment events
are keyed by order and carry no `user_id`, so every ORDER event upserted
this projection to arm that one join. Roughly six writes per order to serve
the single message the payments topic produces.

Refunds now run as RefundNotificationWorkflow (ADR-0040), which asks order
directly and lets Temporal own the retry — so an order outage delays the
bell entry instead of parking it on the DLQ.

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
    op.drop_table("order_recipients")


def downgrade() -> None:
    # The table comes back empty. It was pure projection — rebuilt by
    # replaying the orders topic, which is how it was ever populated.
    op.create_table(
        "order_recipients",
        sa.Column("order_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("restaurant_id", sa.Text, nullable=False),
    )
