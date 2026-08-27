"""Event → notification policy, as pure functions.

This is the product decision of the service: WHO hears about WHICH order
facts, in what words. Kept free of I/O so the whole matrix is table-testable;
the consumer handler owns persistence.

Deliberate silences: OrderPlaced (the customer just did it themselves, and
the kitchen must not see orders before payment clears), OrderSettled,
PaymentAuthorized, PaymentCaptured (internal money movements). The wire
grows a voice only where a human would act on it.
"""

from dataclasses import dataclass
from typing import Any, Literal

from smartfood_kafka import EventType


@dataclass(frozen=True)
class Draft:
    recipient_type: Literal["customer", "restaurant"]
    recipient_id: str
    kind: str
    title: str
    body: str


# The payment events that mint anything at all. The consumer checks this
# BEFORE the recipients-projection lookup, so a no-op PaymentAuthorized/
# PaymentCaptured can never trip a spurious ProjectionLag.
NOTIFYING_PAYMENT_EVENTS = frozenset({EventType.REFUND_PROCESSED})


def money(cents: int, currency: str) -> str:
    amount = f"{cents // 100}.{cents % 100:02d}"
    return f"${amount}" if currency == "USD" else f"{amount} {currency}"


# Customer-facing phrasing per cancel reason (order/values.py CancelReason).
# Pre-pickup cancels never captured money, so "not charged" is a fact, not
# a promise; the customer's own cancel makes no charge claim (the refund
# story, when there is one, arrives via RefundProcessed).
_CANCEL_BODIES = {
    "customer_cancelled": "Your order at {name} is cancelled, as you asked.",
    "restaurant_rejected": "{name} couldn't take your order. Your card was not charged.",
    "restaurant_timeout": "{name} didn't respond in time. Your card was not charged.",
    "payment_declined": "Your card was declined, so the order never reached {name}.",
    "item_unavailable": "Some items just sold out at {name}. Your card was not charged.",
    "at_capacity": "{name} can't take more orders right now. Your card was not charged.",
    # Pre-confirmation only, and the unwind voids any hold it might have
    # taken — so "not charged" is a fact here too.
    "system_timeout": "We couldn't reach {name} in time, so your order was cancelled. "
    "Your card was not charged.",
    # Dispatch: cooked but uncarried (FR-32). The void ran — "not charged"
    # stays a fact (capture only ever happens after delivery).
    "no_rider_available": "We couldn't find a rider for your {name} order, so it was "
    "cancelled. Your card was not charged.",
}
_CANCEL_FALLBACK = "Your order at {name} was cancelled."


def order_drafts(event_type: str, payload: dict[str, Any]) -> list[Draft]:
    """Full-state order events carry everything the copy needs — user_id,
    restaurant_id, restaurant_name, items, totals — by contract."""
    name = payload["restaurant_name"]
    user_id = payload["user_id"]
    # The kitchen mailbox is the BRAND (one bell across every branch,
    # ADR-0028); pre-brands events lack the key and address the branch —
    # exactly the old behavior, healed once the order's next event carries
    # the brand.
    restaurant_id = payload.get("brand_id") or payload["restaurant_id"]

    if event_type == EventType.ORDER_CONFIRMED:
        count = len(payload["items"])
        items = f"{count} item" if count == 1 else f"{count} items"
        total = money(payload["totals"]["total_cents"], payload["totals"]["currency"])
        return [
            Draft(
                "restaurant",
                restaurant_id,
                "order_confirmed",
                "New order to accept",
                f"{items}, {total} — accept or reject before the timer cancels it.",
            ),
            Draft(
                "customer",
                user_id,
                "order_confirmed",
                "Order confirmed",
                f"{name} has been notified and should accept shortly.",
            ),
        ]

    if event_type == EventType.ORDER_CANCELLED:
        reason = payload.get("cancel_reason") or ""
        body = _CANCEL_BODIES.get(reason, _CANCEL_FALLBACK).format(name=name)
        drafts = [Draft("customer", user_id, "order_cancelled", "Order cancelled", body)]
        if reason == "customer_cancelled":
            # The kitchen only hears about cancels it could be cooking for.
            drafts.append(
                Draft(
                    "restaurant",
                    restaurant_id,
                    "order_cancelled",
                    "Order cancelled by the customer",
                    "Stop preparing it — the slot and stock are released.",
                )
            )
        elif reason == "no_rider_available":
            # The kitchen COOKED this one — it deserves to know why the
            # food has no ride, not just that the order vanished.
            drafts.append(
                Draft(
                    "restaurant",
                    restaurant_id,
                    "order_cancelled",
                    "No rider available",
                    "We couldn't find a courier in time — the order was cancelled "
                    "and the slot released.",
                )
            )
        return drafts

    if event_type == EventType.ORDER_DELIVERED:
        return [
            Draft(
                "customer",
                user_id,
                "order_delivered",
                "Order delivered",
                f"Your order from {name} has arrived — enjoy!",
            )
        ]

    return []


def payment_drafts(event_type: str, payload: dict[str, Any], *, user_id: str) -> list[Draft]:
    """Payment payloads have no user_id (keyed by order) — the caller joins
    it in via the order_recipients projection."""
    if event_type == EventType.REFUND_PROCESSED:
        total = money(payload["amount_cents"], payload["currency"])
        return [
            Draft(
                "customer",
                user_id,
                "refund_processed",
                "Refund on its way",
                f"{total} is heading back to your card.",
            )
        ]
    return []
