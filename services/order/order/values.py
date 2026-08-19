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
from typing import Any, Literal


@dataclass(frozen=True)
class LineSnapshot:
    """One priced cart line, frozen at placement. The name and unit price
    are SNAPSHOTS — the menu may change a second later and this order must
    still render exactly what the customer agreed to."""

    menu_item_id: str
    name: str
    unit_price_cents: int
    qty: int
    line_total_cents: int
    options: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PlacementInput:
    """Everything the create_order activity needs to write the order row —
    the whole priced request, resolved and validated by the API before the
    workflow ever started (ADR-0023).

    `request_hash` is the sha256 of the placement body, stamped onto the
    orders row so a retried key can be checked against the cart it was
    minted for (ADR-0024). `placed_at` is an ISO stamp taken by the API,
    not by the activity: an activity that commits and then times out gets
    retried, and a retry must write byte-identical rows — a fresh now()
    inside the activity would not."""

    order_id: str
    request_hash: str
    user_id: str
    restaurant_id: str
    restaurant_name: str
    card_token: str
    menu_version: int
    currency: str
    amount_cents: int
    placed_at: str
    lines: list[LineSnapshot] = field(default_factory=list)
    pricing_snapshot: dict[str, Any] = field(default_factory=dict)
    address_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlacementAck:
    """What the placement update hands back to the waiting HTTP request:
    the order exists and here is its status. This is the 202's contents."""

    order_id: str
    status: str


@dataclass(frozen=True)
class WorkflowInput:
    placement: PlacementInput
    accept_timeout_s: int = 180  # the restaurant_decision window (FR-18)
    pickup_delay_s: int = 20  # S6's simulated delivery timers
    dropoff_delay_s: int = 30
    # How long the PRE-CONFIRMED forward steps may spend retrying before the
    # saga gives up and unwinds. Bounded on purpose: those steps hold a stock
    # reservation whose reaper releases it at 1800s, so an unbounded retry
    # could still be trying to authorize a card against stock somebody else
    # has since bought. Compensations are NOT bounded by this — giving up on
    # an unwind is how money gets stranded.
    forward_deadline_s: int = 300


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
    """What the money/stock activities need. Derived from the placement the
    API already priced — never recomputed (FR-16/flag #2)."""

    restaurant_id: str
    amount_cents: int
    currency: str
    card_token: str
    lines: list[LineSpec] = field(default_factory=list)


def price_of(placement: PlacementInput) -> PriceResult:
    """Projection, not computation — pure enough to run inside the workflow
    sandbox, which is why the price_order activity no longer exists: the
    workflow was told the numbers, so it need not go and read them back."""
    return PriceResult(
        restaurant_id=placement.restaurant_id,
        amount_cents=placement.amount_cents,
        currency=placement.currency,
        card_token=placement.card_token,
        lines=[LineSpec(item_id=line.menu_item_id, qty=line.qty) for line in placement.lines],
    )


# Business outcomes travel as VALUES (never exceptions): control flow on
# values keeps workflow replay deterministic.
ReserveResult = Literal["ok", "item_unavailable", "at_capacity"]
AuthResult = Literal["ok", "declined"]

Verdict = Literal["accept", "reject"]

SIGNAL_RESTAURANT_DECISION = "restaurant_decision"
SIGNAL_FOOD_READY = "food_ready"  # kitchen → DeliveryWorkflow (dlv::{order_id})
SIGNAL_CANCEL_REQUESTED = "cancel_requested"  # customer → OrderWorkflow (S7)

# The placement handshake (ADR-0023). A signal could not do this: signals
# are fire-and-forget, and the HTTP request needs an ANSWER — the order id
# and its status, once the row exists. Updates are the request/response
# member of the family, so placement rides one.
UPDATE_AWAIT_PLACEMENT = "await_placement"

# The workflow refuses a customer cancel from here on: once the courier
# holds the food, it is coming (FR-21). The API mirrors this set.
CancelOutcome = Literal["ok", "too_late"]


class ActivityName(StrEnum):
    CREATE_ORDER = "create_order"  # the saga's first act (ADR-0023)
    VALIDATE_AND_RESERVE = "validate_and_reserve"
    AUTHORIZE_PAYMENT = "authorize_payment"
    CONFIRM_ORDER = "confirm_order"
    MARK_ACCEPTED = "mark_accepted"
    MARK_PICKED_UP = "mark_picked_up"
    MARK_DELIVERED = "mark_delivered"
    CAPTURE_PAYMENT = "capture_payment"
    SETTLE_ORDER = "settle_order"
    BEGIN_CANCEL = "begin_cancel"
    TRY_BEGIN_CANCEL = "try_begin_cancel"  # set-guarded: kitchen-window cancel vs courier race
    VOID_AUTHORIZATION = "void_authorization"
    RELEASE_RESERVATION = "release_reservation"
    FINISH_CANCEL = "finish_cancel"


class CancelReason(StrEnum):
    ITEM_UNAVAILABLE = "item_unavailable"
    AT_CAPACITY = "at_capacity"
    PAYMENT_DECLINED = "payment_declined"
    RESTAURANT_REJECTED = "restaurant_rejected"
    RESTAURANT_TIMEOUT = "restaurant_timeout"
    CUSTOMER_CANCELLED = "customer_cancelled"
    # The saga could not finish a pre-confirmation step inside its deadline
    # (inventory or payment unreachable). Never reaches the kitchen, so the
    # kitchen's decision matrix treats it like the other pre-kitchen deaths.
    SYSTEM_TIMEOUT = "system_timeout"
