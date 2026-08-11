"""inventory_db schema — docs/service-ownership.md (Inventory row).

SQLAlchemy Core tables: the single source both the Alembic migration and the
test create_all derive from. Must stay sqlite-compatible for the unit suite.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

# One row per menu item — STRICT stock (user decision): rows start at 0 and
# an item cannot sell until its admin sets stock. Rows are auto-created by
# the catalog.changes consumer; a missing row is treated as 0 at reserve
# time (consumer-lag safety, same 409).
stock = sa.Table(
    "stock",
    metadata,
    sa.Column("item_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False, index=True),
    sa.Column("available", sa.Integer, nullable=False, server_default="0"),
    # Outbox event_id determinism: every mutation bumps it.
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("available >= 0", name="ck_stock_available_nonneg"),
)

# Kitchen slots as stock (FR-15): the conditional increment
# `WHERE active < capacity` is the capacity gate. Deliberately NO
# `active <= capacity` CHECK — lowering capacity below current active must
# stay legal (stops NEW orders; running ones drain naturally).
restaurant_load = sa.Table(
    "restaurant_load",
    metadata,
    sa.Column("restaurant_id", sa.Text, primary_key=True),
    sa.Column("active", sa.Integer, nullable=False, server_default="0"),
    sa.Column("capacity", sa.Integer, nullable=False, server_default="10"),
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint("active >= 0", name="ck_load_active_nonneg"),
)

# Reservation ledger. PK = order_id: retries of the same order's reserve are
# idempotent by construction — no idempotency machinery needed here.
reservations = sa.Table(
    "reservations",
    metadata,
    sa.Column("order_id", sa.Text, primary_key=True),
    sa.Column("restaurant_id", sa.Text, nullable=False),
    sa.Column("lines", sa.JSON, nullable=False),  # [{"item_id": ..., "qty": n}]
    # active -> released (compensation) | consumed (settle) | expired (reaper)
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("version", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint(
        "status IN ('active','released','consumed','expired')", name="ck_reservations_status"
    ),
)
sa.Index("ix_reservations_reaper", reservations.c.status, reservations.c.expires_at)

# Same column contract as catalog's outbox (smartfood_outbox poller docstring).
outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("aggregate_type", sa.Text, nullable=False),  # "stock" | "reservation"
    sa.Column("aggregate_id", sa.Text, nullable=False),
    sa.Column("aggregate_version", sa.Integer, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True, index=True),
    sa.Column("traceparent", sa.Text, nullable=True),
)
