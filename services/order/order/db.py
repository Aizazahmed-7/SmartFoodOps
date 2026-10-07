"""order_db schema — docs/service-ownership.md (Order row).

Everything a placed order must survive: menu edits (name/price snapshots on
order_items), address deletion (delivery_address_snapshot), and pricing
changes (pricing_snapshot) — an order is a historical fact, not a view.
"""

from typing import Literal, get_args

import sqlalchemy as sa
from smartfood_outbox import outbox_table

from .values import CancelReason

metadata = sa.MetaData()

# The cancellation vocabulary, derived from the enum the saga actually
# raises — the same single-source idiom as OrderStatus below. StrEnum
# members ARE their values, so the tuple repr is a valid SQL IN list.
CANCEL_REASONS: tuple[str, ...] = tuple(str(reason) for reason in CancelReason)

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

RATEABLE_STATUSES: tuple[str, ...] = ("DELIVERED", "SETTLED")
"""FR-91: a customer may rate an order that arrived.

SETTLED is included because it is DELIVERED plus money — to the person who
ate the food they are the same evening, and excluding it would mean the
window to leave feedback closed the moment the card was captured.

REFUNDED is deliberately absent. It is reachable only from DELIVERED, so
the food did arrive — but a refund means something went wrong that this
scale cannot express, and inviting a star rating there asks the wrong
question. Feedback on a refunded order belongs to support, not to a corpus
that FR-92 will summarise as how the kitchen is doing.
"""

orders = sa.Table(
    "orders",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    # The branch's brand (ADR-0028), stamped from the pricing snapshot at
    # placement; the kitchen feed scopes on it (a brand claim sees every
    # branch's orders). Nullable forever: pre-brand rows legitimately
    # predate the concept until the catalog.changes repoint handler fills
    # them; the feed's OR arm covers the gap.
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("restaurant_name_snapshot", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="PLACED"),
    sa.Column("payment_method", sa.Text, nullable=False, server_default="CARD"),
    # No card_token here. The instrument reaches Payment through the
    # workflow input, and Payment keeps its own copy for its own lifecycle
    # — an unread third copy in this database was retention, not record
    # (review 2026-09-15). Capture and void key on the PSP ref, not the
    # token, so nothing downstream ever needed it back.
    # sha256 of the exact placement body this order was created from
    # (ADR-0024). The order row IS the idempotency record now: a retry
    # re-derives this order_id and reads the row — same hash → replay the
    # 202, different hash → 422 (a client reused a key across carts).
    # Nullable: rows born before ADR-0024 skip the guard.
    sa.Column("request_hash", sa.Text, nullable=True),
    # {subtotal,discount,fee,tax,total}_cents + currency — FR-16: authorization
    # and refunds are computed ONLY from this, never recomputed.
    sa.Column("pricing_snapshot", sa.JSON, nullable=False),
    # {address_id,label,line1,city,lat,lon} — survives address deletion.
    sa.Column("delivery_address_snapshot", sa.JSON, nullable=False),
    # The courier, stamped when dispatch's accept lands (RECORD_RIDER).
    # NULL until assigned — and forever, for orders that die earlier.
    # Full-state events carry it from that point on, which is how
    # analytics learns per-rider delivery spans without a stream join.
    sa.Column("rider_id", sa.Text, nullable=True),
    sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # The milestones (FR-81). `updated_at` is overwritten by every move, so
    # it can say WHEN something last happened but never WHAT took long.
    # Stamped by transition() inside the guarded UPDATE, which is what makes
    # them exactly-once: a replayed transition matches 0 rows and therefore
    # cannot re-stamp a moment that already passed.
    sa.Column("payment_cleared_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("preparing_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("ready_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("picked_up_at", sa.TIMESTAMP(timezone=True), nullable=True),
    # tuple repr renders as ('PLACED', 'VALIDATED', ...) — valid SQL IN list.
    sa.CheckConstraint(f"status IN {STATUSES!r}", name="ck_orders_status"),
    sa.CheckConstraint("payment_method IN ('CARD','COD')", name="ck_orders_payment_method"),
)
# Customer history: keyset pagination walks this exactly (api-standards §5).
sa.Index("ix_orders_history", orders.c.user_id, orders.c.placed_at.desc(), orders.c.order_id.desc())
# Restaurant feed (S6): status-filtered kitchen queues.
sa.Index("ix_orders_feed", orders.c.restaurant_id, orders.c.status, orders.c.placed_at)
# Brand feed (ADR-0028): the same queue walked by the owner's brand claim.
sa.Index("ix_orders_feed_brand", orders.c.brand_id, orders.c.status, orders.c.placed_at)
# (ix_orders_sweeper lived here until ADR-0023. Nothing scans for orphaned
# PLACED rows any more: the workflow creates the order, so an order cannot
# exist without one. Migration 0003 drops it.)

# ── cancellation (review 2026-09-15) ───────────────────────────────
# Was `orders.cancel_reason`, a nullable column NULL on every order that
# completed. Split out once a second field appeared: the row's EXISTENCE is
# now the fact, `reason` is NOT NULL because a cancellation without one is
# meaningless, and the two can never disagree with the orders row about
# whether a cancellation happened.
#
# Deliberately three columns. `cancelled_by` is derivable from `reason`
# (customer_cancelled / restaurant_rejected / system_timeout /
# no_rider_available each name their actor), a refund column would be
# speculative — capture-after-delivery makes the customer refund path
# structurally unreachable — and the unwind's progress belongs to Temporal,
# not to a second copy here.
order_cancellations = sa.Table(
    "order_cancellations",
    metadata,
    sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), primary_key=True),
    sa.Column("reason", sa.Text, nullable=False),
    # The moment the cancellation was DECIDED, not the moment the unwind
    # finished: `begin_cancel` stamps the reason at CANCELLING and the
    # compensations can hold that state for an unbounded window
    # (activities.py). The row is written once and never moved — the
    # CANCELLED transition re-writes the same reason and must not disturb
    # this. NOTE this is a different instant from analytics'
    # `order_facts.cancelled_at`, which is stamped from the terminal
    # OrderCancelled event; the gap between them IS the unwind duration.
    sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=False),
    # The vocabulary its readers branch on (kitchen's decision matrix) and
    # count with (analytics' rejection rate) — closed, and now enforced.
    sa.CheckConstraint(f"reason IN {CANCEL_REASONS!r}", name="ck_order_cancellations_reason"),
)

# FR-91. Part A captured no feedback at all, so there was nothing to
# summarise and no honest proxy for it.
order_feedback = sa.Table(
    "order_feedback",
    metadata,
    # One row per order, as the schema rather than as a rule somebody
    # remembers. A customer changing their mind rewrites this row; the
    # summary is regenerated from whatever the rows currently say, which is
    # the only version anyone can defend quoting.
    sa.Column("order_id", sa.Text, sa.ForeignKey("orders.order_id"), primary_key=True),
    # Who said it — the ownership check on every read and write, and the
    # reason a summary can promise it quotes only real customers.
    sa.Column("user_id", sa.Text, nullable=False),
    # Snapshotted from the order, not joined. A summary is scoped to one
    # restaurant's own rows, and a branch can be repointed to a brand later
    # (ADR-0028); feedback about the food served that night belongs to the
    # branch that served it.
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("rating", sa.Integer, nullable=False),
    # Optional, and most ratings will not have one. A summary that needs
    # quotes has to cope with a corpus that is mostly stars.
    sa.Column("comment", sa.Text, nullable=True),
    sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_order_feedback_rating"),
    # BOTH, because the read is `restaurant_id = claim OR brand_id = claim`
    # — a claim names either (ADR-0028) and an OR across two columns cannot
    # use one of them. With both, the planner takes a BitmapOr instead of
    # scanning the table on every studio mount.
    sa.Index("ix_order_feedback_restaurant", "restaurant_id", "submitted_at"),
    sa.Index("ix_order_feedback_brand", "brand_id", "submitted_at"),
)

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

# (idempotency_keys lived here until ADR-0024. Placement's idempotency
# record is the orders row itself — the derived order_id is the key, and
# request_hash is the body guard. Migration 0004 drops the table; the
# smartfood-idempotency library lives on in payment, where stored 402
# replays and PSP read-before-execute genuinely need it.)

# The 9-column contract lives with its reader (smartfood-outbox).
outbox = outbox_table(metadata)
