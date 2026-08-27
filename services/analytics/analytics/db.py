"""analytics_db — one FACT row per order, folded from lifecycle events.

Design over the obvious alternative (a counters table): counters cannot
absorb at-least-once redelivery — `orders = orders + 1` applied twice is a
lie, and no natural key saves an increment. A fact row CAN: every event
writes absolute values keyed by order_id, so a redelivered batch converges
to the same row. Aggregates (daily rollups, rates, peaks) are computed at
READ time from facts; materializing them back into tables is the named
scale knob, done then as periodic recomputation — never as increments.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

order_facts = sa.Table(
    "order_facts",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False, index=True),
    # The branch's brand (ADR-0028), from the event payload; NULL for facts
    # projected before the cutover until the repoint consumer heals them.
    # The owner's read API scopes on (brand_id OR restaurant_id).
    sa.Column("brand_id", sa.Text, nullable=True, index=True),
    sa.Column("user_id", sa.Text, nullable=False),
    # The LATEST lifecycle state seen. Per-order ordering is guaranteed by
    # the topic key (= order_id → one partition), so last-write-wins here
    # is genuinely last-event-wins.
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("aggregate_version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("total_cents", sa.Integer, nullable=False, server_default="0"),
    # One timestamp per milestone the metrics need. NULL = not reached.
    sa.Column("placed_at", sa.TIMESTAMP(timezone=True), nullable=True, index=True),
    sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    # The courier (dispatch milestone): stamped by order events once
    # assigned — the per-rider delivery spans FR-43's utilization needs.
    sa.Column("rider_id", sa.Text, nullable=True),
    sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("cancel_reason", sa.Text, nullable=True),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# One row per SAMPLED menu view (S8). view_id is uuid5(request_id) minted at
# the emitter, so at-least-once redelivery collapses on this PK — the same
# natural-key dedupe as everything else, applied to telemetry. user_id NULL
# = anonymous browser: counts toward volume, excluded from conversion (you
# cannot join an order to a browser you cannot name).
menu_views = sa.Table(
    "menu_views",
    metadata,
    sa.Column("view_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("brand_id", sa.Text, nullable=True),
    sa.Column("user_id", sa.Text, nullable=True),
    sa.Column("viewed_at", sa.TIMESTAMP(timezone=True), nullable=False),
)
sa.Index("ix_menu_views_restaurant_time", menu_views.c.restaurant_id, menu_views.c.viewed_at)
