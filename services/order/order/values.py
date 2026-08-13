"""Pure value objects shared by the workflow and its activities.

This module is imported INSIDE the Temporal workflow sandbox
(workflows.py), so it must stay dependency-free: stdlib dataclasses and
typing only. Workflow and signal arguments carry IDs and small value
objects — never PII, tokens beyond the card token reference, or blobs
(DoD-3).

Activities are invoked BY NAME (the ActivityName constants) so the
workflow never imports activities.py — that module pulls in SQLAlchemy
and httpx, which have no business inside the deterministic sandbox.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True)
class WorkflowInput:
    order_id: str
    accept_timeout_s: int = 180  # the restaurant_decision window (FR-18)
    pickup_delay_s: int = 20  # S6's simulated delivery timers
    dropoff_delay_s: int = 30


@dataclass(frozen=True)
class DeliveryInput:
    """DeliveryWorkflow (child) input — the simulated courier's timings.
    Real dispatch replaces the timers next milestone; the id contract
    (dlv::{order_id}) and signal names are the part that must not change."""

    order_id: str
    pickup_delay_s: int = 20
    dropoff_delay_s: int = 30


@dataclass(frozen=True)
class LineSpec:
    item_id: str
    qty: int


@dataclass(frozen=True)
class PriceResult:
    """price_order (local activity) output: everything later activities
    need, loaded from the placement snapshot in ONE read — never
    recomputed (FR-16/flag #2)."""

    restaurant_id: str
    amount_cents: int
    currency: str
    card_token: str
    lines: list[LineSpec] = field(default_factory=list)


# Business outcomes travel as VALUES (never exceptions): control flow on
# values keeps workflow replay deterministic.
ReserveResult = Literal["ok", "item_unavailable", "at_capacity"]
AuthResult = Literal["ok", "declined"]

Verdict = Literal["accept", "reject"]

SIGNAL_RESTAURANT_DECISION = "restaurant_decision"
SIGNAL_FOOD_READY = "food_ready"  # kitchen → DeliveryWorkflow (dlv::{order_id})


class ActivityName(StrEnum):
    PRICE_ORDER = "price_order"
    VALIDATE_AND_RESERVE = "validate_and_reserve"
    AUTHORIZE_PAYMENT = "authorize_payment"
    CONFIRM_ORDER = "confirm_order"
    MARK_ACCEPTED = "mark_accepted"
    MARK_PICKED_UP = "mark_picked_up"
    MARK_DELIVERED = "mark_delivered"
    CAPTURE_PAYMENT = "capture_payment"
    SETTLE_ORDER = "settle_order"
    BEGIN_CANCEL = "begin_cancel"
    VOID_AUTHORIZATION = "void_authorization"
    RELEASE_RESERVATION = "release_reservation"
    FINISH_CANCEL = "finish_cancel"


class CancelReason(StrEnum):
    ITEM_UNAVAILABLE = "item_unavailable"
    AT_CAPACITY = "at_capacity"
    PAYMENT_DECLINED = "payment_declined"
    RESTAURANT_REJECTED = "restaurant_rejected"
    RESTAURANT_TIMEOUT = "restaurant_timeout"
