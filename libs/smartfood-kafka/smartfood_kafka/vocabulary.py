"""The event vocabulary — every event type and topic name, as symbols.

These strings are WIRE CONTRACT: a producer writes "RestaurantCreated" and
a consumer in another service matches on it. As raw literals, a typo on
either side is a silent no-op (the most dangerous failure shape there is);
as enum members, a typo is an AttributeError at import time.

StrEnum members ARE strings: they serialize into the Avro envelope, hash
into deterministic event ids, and compare equal to raw strings unchanged —
adopting them changes zero bytes on the wire.

Grows only alongside docs/api-standards.md / ARCHITECTURE §11 — an event
name here is a promise consumers may depend on forever.
"""

from enum import StrEnum


class EventType(StrEnum):
    # catalog — aggregate "restaurant"; payloads carry the FULL menu
    # snapshot (compacted topic: the last event must stand alone)
    RESTAURANT_CREATED = "RestaurantCreated"
    RESTAURANT_UPDATED = "RestaurantUpdated"
    RESTAURANT_PAUSED = "RestaurantPaused"
    RESTAURANT_RESUMED = "RestaurantResumed"
    CATEGORY_ADDED = "CategoryAdded"
    CATEGORY_UPDATED = "CategoryUpdated"
    CATEGORY_DELETED = "CategoryDeleted"
    ITEM_ADDED = "ItemAdded"
    ITEM_UPDATED = "ItemUpdated"
    ITEM_DELETED = "ItemDeleted"

    # inventory — aggregates "stock" and "reservation"
    STOCK_ADJUSTED = "StockAdjusted"
    STOCK_RESERVED = "StockReserved"
    RESERVATION_RELEASED = "ReservationReleased"
    RESERVATION_CONSUMED = "ReservationConsumed"

    # order — aggregate "order"; major states only (plan: no per-transition
    # spam). CONFIRMED/CANCELLED/DELIVERED/SETTLED land with S5/S6.
    ORDER_PLACED = "OrderPlaced"
    ORDER_CONFIRMED = "OrderConfirmed"
    ORDER_CANCELLED = "OrderCancelled"
    ORDER_DELIVERED = "OrderDelivered"
    ORDER_SETTLED = "OrderSettled"

    # payment — aggregate "payment" (doc-mandated names, ADR-0018)
    PAYMENT_AUTHORIZED = "PaymentAuthorized"
    PAYMENT_CAPTURED = "PaymentCaptured"
    REFUND_PROCESSED = "RefundProcessed"


class Topic(StrEnum):
    """Topic suffixes — always composed with a cell via `topic()` (§9:
    every topic name is cell-parameterized from day one)."""

    CATALOG_CHANGES = "catalog.changes"
    ORDERS_EVENTS = "orders.events"
    INVENTORY_EVENTS = "inventory.events"
    PAYMENTS_EVENTS = "payments.events"


def topic(cell_id: str, suffix: Topic) -> str:
    return f"{cell_id}.{suffix}"
