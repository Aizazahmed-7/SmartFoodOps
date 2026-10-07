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

    # order — aggregate "order". Part A published major states only ("no
    # per-transition spam"); Part B's explanation engine amends that,
    # because a stage that never announces itself is a stage no consumer
    # can time (PRD-partb FR-81, ADR-0046). The anti-spam intent is kept
    # where it actually lived: notification's `order_drafts` returns no
    # draft for any of the four below, so no customer gains a push.
    ORDER_PLACED = "OrderPlaced"
    ORDER_CONFIRMED = "OrderConfirmed"
    ORDER_ACCEPTED = "OrderAccepted"
    ORDER_PREPARING = "OrderPreparing"
    ORDER_READY = "OrderReady"
    ORDER_PICKED_UP = "OrderPickedUp"
    ORDER_CANCELLED = "OrderCancelled"
    ORDER_DELIVERED = "OrderDelivered"
    ORDER_SETTLED = "OrderSettled"

    # payment — aggregate "payment" (doc-mandated names, ADR-0018)
    MENU_VIEWED = "MenuViewed"

    PAYMENT_AUTHORIZED = "PaymentAuthorized"
    PAYMENT_CAPTURED = "PaymentCaptured"
    REFUND_PROCESSED = "RefundProcessed"

    # dispatch — aggregates "delivery" (keyed by order) and "rider"
    # (presence sessions). Direct-produced, no outbox (ADR-0026).
    RIDER_LOCATION = "RiderLocation"  # 0.2 Hz downsample (rider-gateway)
    RIDER_ONLINE = "RiderOnline"
    RIDER_OFFLINE = "RiderOffline"
    RIDER_ASSIGNED = "RiderAssigned"
    RIDER_DELIVERY_COMPLETED = "RiderDeliveryCompleted"

    # ai-assistant — aggregate "interaction", one event per answered turn.
    # A product KPI rather than telemetry, so it goes through the outbox
    # like every other business fact (PRD FR-94, ADR-0002).
    ASSISTANT_INTERACTION = "AssistantInteraction"
    # What we put in front of a customer, and what they went on to order.
    # ACCEPTED is derived from the order stream, never reported by a client
    # (FR-79) — a surface that grades its own recommendations is a metric
    # that improves when the client changes.
    RECOMMENDATION_SHOWN = "RecommendationShown"
    RECOMMENDATION_ACCEPTED = "RecommendationAccepted"


class Topic(StrEnum):
    """Topic suffixes — always composed with a cell via `topic()` (§9:
    every topic name is cell-parameterized from day one)."""

    CATALOG_CHANGES = "catalog.changes"
    ORDERS_EVENTS = "orders.events"
    INVENTORY_EVENTS = "inventory.events"
    PAYMENTS_EVENTS = "payments.events"
    # Telemetry, not business facts: browse events skip the outbox entirely
    # (nothing transactional to be atomic with) and tolerate loss.
    BROWSE_EVENTS = "browse.events"
    # Dispatch facts: DDB is the truth, Kafka is the copy — direct produce
    # in dev, DDB Streams in prod (ADR-0026).
    DISPATCH_EVENTS = "dispatch.events"
    RIDER_LOCATIONS = "rider.locations"  # GPS telemetry (downsampled)
    # AI-plane facts. NOT browse.events' shape: those are lossy telemetry
    # with nothing to be atomic with, these are the source of the six
    # required metrics (FR-95) and are staged transactionally.
    ASSISTANT_EVENTS = "assistant.events"


def topic(cell_id: str, suffix: Topic) -> str:
    return f"{cell_id}.{suffix}"
