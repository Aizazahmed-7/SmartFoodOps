"""Move `orders.cancel_reason` into `order_cancellations`.

The column was NULL on every order that completed, and a second field
(`cancelled_at`) had nowhere to go — `orders` has only `updated_at`, which
moves on every later transition, so the moment a cancellation was decided
was unrecoverable from this database.

The row's existence is now the fact: `reason` is NOT NULL, `cancelled_at` is
NOT NULL, and neither can disagree with the other about whether a
cancellation happened. The reason vocabulary gets the CHECK it never had —
kitchen's decision matrix branches on these values and analytics counts
rejections with them.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_REASONS = (
    "item_unavailable",
    "at_capacity",
    "payment_declined",
    "restaurant_rejected",
    "restaurant_timeout",
    "customer_cancelled",
    "system_timeout",
    "no_rider_available",
)


def upgrade() -> None:
    op.create_table(
        "order_cancellations",
        sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), primary_key=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(f"reason IN {_REASONS!r}", name="ck_order_cancellations_reason"),
    )
    # `updated_at` is the closest recoverable stand-in for the decision
    # moment on historical rows: for an order that reached a terminal
    # cancel state it is the last transition, which is at or after the
    # decision. Approximate and knowably so — better than dropping the
    # history, and new rows carry the real instant.
    op.get_bind().execute(
        sa.text("""
        INSERT INTO order_cancellations (order_id, reason, cancelled_at)
        SELECT order_id, cancel_reason, updated_at
          FROM orders
         WHERE cancel_reason IS NOT NULL
    """)
    )
    # A reason outside the vocabulary would have failed the INSERT above,
    # which is the intended outcome: the CHECK is the point of the move.
    op.drop_column("orders", "cancel_reason")


def downgrade() -> None:
    op.add_column("orders", sa.Column("cancel_reason", sa.Text, nullable=True))
    op.get_bind().execute(
        sa.text("""
        UPDATE orders SET cancel_reason = c.reason
          FROM order_cancellations c
         WHERE c.order_id = orders.order_id
    """)
    )
    # cancelled_at has nowhere to go on the way back — orders never had it.
    op.drop_table("order_cancellations")
