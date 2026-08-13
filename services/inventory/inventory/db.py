"""inventory_db schema — docs/service-ownership.md (Inventory row).

SQLAlchemy Core tables: the single source both the Alembic migration and the
test create_all derive from. Must stay sqlite-compatible for the unit suite.
"""

from typing import Literal, get_args

import sqlalchemy as sa
from smartfood_outbox import outbox_table

metadata = sa.MetaData()

ReservationStatus = Literal["active", "released", "consumed", "expired"]
RESERVATION_STATUSES: tuple[str, ...] = get_args(ReservationStatus)

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
    sa.CheckConstraint(f"status IN {RESERVATION_STATUSES!r}", name="ck_reservations_status"),
)
sa.Index("ix_reservations_reaper", reservations.c.status, reservations.c.expires_at)

# The 9-column contract lives with its reader (smartfood-outbox).
outbox = outbox_table(metadata)
