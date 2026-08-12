"""order_db schema — docs/service-ownership.md (Order row).

Everything a placed order must survive: menu edits (name/price snapshots on
order_items), address deletion (delivery_address_snapshot), and pricing
changes (pricing_snapshot) — an order is a historical fact, not a view.
"""

from typing import Literal, get_args

import sqlalchemy as sa
from smartfood_idempotency import idempotency_table

metadata = sa.MetaData()

# The state machine's vocabulary (ARCHITECTURE §6.2). The Literal is the
# single source of truth: every transition signature is checked against it
# at type-check time, and the CHECK constraint below is derived from it.
OrderStatus = Literal[
    "PLACED",
    "VALIDATED",
    "PAYMENT_CLEARED",
    "CONFIRMED",
    "ACCEPTED",
    "PREPARING",
    "READY",
    "PICKED_UP",
    "DELIVERED",
    "SETTLED",
    "CANCELLING",
    "CANCELLED",
    "REFUNDED",
]
STATUSES: tuple[str, ...] = get_args(OrderStatus)

orders = sa.Table(
    "orders",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("restaurant_name_snapshot", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="PLACED"),
    # Bumped by the guarded transition helper (S5) — outbox event identity.
    sa.Column("aggregate_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("payment_method", sa.Text, nullable=False, server_default="CARD"),
    sa.Column("card_token", sa.Text, nullable=False),
    sa.Column("menu_version", sa.Integer, nullable=False),
    # {subtotal,discount,fee,tax,total}_cents + currency — FR-16: authorization
    # and refunds are computed ONLY from this, never recomputed.
    sa.Column("pricing_snapshot", sa.JSON, nullable=False),
    # {address_id,label,line1,city,lat,lon} — survives address deletion.
    sa.Column("delivery_address_snapshot", sa.JSON, nullable=False),
    sa.Column("cancel_reason", sa.Text, nullable=True),
    sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # tuple repr renders as ('PLACED', 'VALIDATED', ...) — valid SQL IN list.
    sa.CheckConstraint(f"status IN {STATUSES!r}", name="ck_orders_status"),
    sa.CheckConstraint("payment_method IN ('CARD','COD')", name="ck_orders_payment_method"),
)
# Customer history: keyset pagination walks this exactly (api-standards §5).
sa.Index("ix_orders_history", orders.c.user_id, orders.c.placed_at.desc(), orders.c.order_id.desc())
# Restaurant feed (S6): status-filtered kitchen queues.
sa.Index("ix_orders_feed", orders.c.restaurant_id, orders.c.status, orders.c.placed_at)

order_items = sa.Table(
    "order_items",
    metadata,
    sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), primary_key=True),
    sa.Column("line_no", sa.Integer, primary_key=True),
    sa.Column("menu_item_id", sa.Text, nullable=False),
    sa.Column("name_snapshot", sa.Text, nullable=False),
    sa.Column("unit_price_cents", sa.Integer, nullable=False),
    sa.Column("qty", sa.Integer, nullable=False),
    # [{group_id, group_name, option_id, name, price_delta_cents}] — the
    # receipt renders from here forever, whatever happens to the menu.
    sa.Column("options_snapshot", sa.JSON, nullable=False),
    sa.Column("line_total_cents", sa.Integer, nullable=False),
    sa.CheckConstraint("qty BETWEEN 1 AND 50", name="ck_order_items_qty"),
)

idempotency_keys = idempotency_table(metadata)

# Same column contract as every outbox (smartfood_outbox poller docstring).
outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("aggregate_type", sa.Text, nullable=False),  # "order"
    sa.Column("aggregate_id", sa.Text, nullable=False),
    sa.Column("aggregate_version", sa.Integer, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True, index=True),
    sa.Column("traceparent", sa.Text, nullable=True),
)
