"""Why is my order where it is? (FR-82, FR-84)

A pure function over facts. No I/O, no model, no clock of its own — `now`
is an argument, because a resolver that reads the wall clock cannot be
tested over the branch where an order has been READY for nine minutes.

**The LLM is never asked WHY.** It is asked to phrase an answer whose
content is already decided here. That split is the whole design: a model
that is handed "reason=AWAITING_COURIER, ready 9 minutes ago, deadline 10
minutes" can write a good sentence and cannot invent a cause, whereas a
model handed a raw order row will confidently explain a delay that never
happened. Every judgement in this milestone lives in this module, once, so
it can be enumerated and tested rather than discovered in production.

Two rules run through everything below:

**Unsure is an answer.** UC-22 is explicit — the resolver unsure means
`UNKNOWN` and a hand-off to support, never a guess. A wrong explanation is
worse than an absent one, because the customer acts on it.

**A stage is only late against a real budget.** `overdue` is `None`, not
`False`, wherever no timer governs the stage. The kitchen has no cooking
budget (Part A's FR-55 wants prep time; it does not exist yet) and there is
no road-time model (OSRM ETAs stay deferred). Reporting "not late" for
those would be a claim nothing supports, so the type makes the third case
impossible to ignore.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .ports import KitchenLoad


class ReasonCode(StrEnum):
    """The closed set. A renderer may ship a template per member and be
    sure it has covered every answer this resolver can give."""

    # ── in flight ──────────────────────────────────────────────────
    PAYMENT_IN_PROGRESS = "payment_in_progress"
    AWAITING_RESTAURANT = "awaiting_restaurant"
    KITCHEN_NOT_STARTED = "kitchen_not_started"
    KITCHEN_PREPARING = "kitchen_preparing"
    KITCHEN_BUSY = "kitchen_busy"
    AWAITING_COURIER = "awaiting_courier"
    COURIER_ON_THE_WAY = "courier_on_the_way"
    IN_TRANSIT = "in_transit"

    # ── finished ───────────────────────────────────────────────────
    DELIVERED = "delivered"
    REFUNDED = "refunded"

    # ── cancelled, by cause (FR-86 renders these) ──────────────────
    CANCELLED_BY_CUSTOMER = "cancelled_by_customer"
    CANCELLED_BY_RESTAURANT = "cancelled_by_restaurant"
    CANCELLED_RESTAURANT_SILENT = "cancelled_restaurant_silent"
    CANCELLED_ITEM_UNAVAILABLE = "cancelled_item_unavailable"
    CANCELLED_KITCHEN_FULL = "cancelled_kitchen_full"
    CANCELLED_PAYMENT_DECLINED = "cancelled_payment_declined"
    CANCELLED_NO_COURIER = "cancelled_no_courier"
    CANCELLED_SYSTEM = "cancelled_system"

    # ── the honest exit ────────────────────────────────────────────
    UNKNOWN = "unknown"


_PRE_AUTHORIZATION_REASONS = frozenset({"payment_declined", "item_unavailable", "at_capacity"})
"""Cancellations that provably happened BEFORE any money was held.

A declined card is an authorization that failed; a stock or capacity
refusal happens in `validate_and_reserve`, before payment is called at all.
For these three the answer is knowable without any milestone, which matters
because rows predating the milestone columns have no other evidence.
"""


def _payment_held(timeline: "Timeline") -> bool | None:
    """Did this order hold money? True, False, or "we cannot tell".

    `payment_cleared_at` is the evidence: `authorize_payment` moves an order
    to PAYMENT_CLEARED only on a successful authorization. It is NOT
    `confirmed_at` — that is one transition later, and every order cancelled
    at PAYMENT_CLEARED holds real money on a real card while `confirmed_at`
    is null.

    None is the important third case and the reason this is not a bool.
    Rows placed before these columns existed carry no milestones at all
    (they are never backfilled, ADR-0046), so for them the honest answer is
    that nobody recorded it — and an explanation that cannot tell must not
    pick a side. Telling a customer "you won't be charged" when a hold is
    sitting on their card is the worst sentence this engine can produce.
    """
    if timeline.payment_cleared_at is not None:
        return True
    if (timeline.cancel_reason or "") in _PRE_AUTHORIZATION_REASONS:
        return False
    # Any milestone at all means this row is new enough to be trusted, so a
    # missing `payment_cleared_at` really does mean it never got that far.
    if any(
        stamp is not None
        for stamp in (
            timeline.confirmed_at,
            timeline.accepted_at,
            timeline.preparing_at,
            timeline.ready_at,
            timeline.picked_up_at,
        )
    ):
        return True  # reached CONFIRMED or beyond — authorization preceded it
    return None


_BY_CANCEL_REASON: Mapping[str, ReasonCode] = {
    "customer_cancelled": ReasonCode.CANCELLED_BY_CUSTOMER,
    "restaurant_rejected": ReasonCode.CANCELLED_BY_RESTAURANT,
    "restaurant_timeout": ReasonCode.CANCELLED_RESTAURANT_SILENT,
    "item_unavailable": ReasonCode.CANCELLED_ITEM_UNAVAILABLE,
    "at_capacity": ReasonCode.CANCELLED_KITCHEN_FULL,
    "payment_declined": ReasonCode.CANCELLED_PAYMENT_DECLINED,
    "no_rider_available": ReasonCode.CANCELLED_NO_COURIER,
    "system_timeout": ReasonCode.CANCELLED_SYSTEM,
}
"""Order's `CancelReason` values, spelled out rather than imported.

Importing order's enum would make the assistant depend on another service's
package, which the layer-contract scan forbids and ADR-0003 forbids for
better reasons than tidiness. The cost is that a NEW cancel reason lands
here as an unmapped string — which resolves to UNKNOWN and a support
hand-off, the correct behaviour for a cause this module has never heard of.
"""


@dataclass(frozen=True)
class Timeline:
    """The order's own facts. Every field is a moment that was stamped by
    the transition that caused it (ADR-0046), so None means "never reached
    or never recorded" and is never rounded to a number."""

    status: str
    placed_at: datetime
    payment_cleared_at: datetime | None = None
    confirmed_at: datetime | None = None
    accepted_at: datetime | None = None
    preparing_at: datetime | None = None
    ready_at: datetime | None = None
    picked_up_at: datetime | None = None
    cancel_reason: str | None = None


@dataclass(frozen=True)
class Delivery:
    """What dispatch knows. `None` for the whole value means dispatch was
    not reachable or has no row — not that no courier exists."""

    state: str
    assigned_at: datetime | None = None


@dataclass(frozen=True)
class Budget:
    """The Temporal timer budget, mirrored from order's Settings.

    Defaults match Part A's today. They are arguments rather than constants
    because the explanation must describe the deadline the order is ACTUALLY
    running under — an operator who widened `no_rider_deadline_s` for a
    holiday has changed what "late" means, and a hardcoded 600 here would
    keep telling customers the old story.
    """

    accept_timeout_s: int = 180
    no_rider_deadline_s: int = 600
    pickup_timeout_s: int = 300
    forward_deadline_s: int = 300


@dataclass(frozen=True)
class Eta:
    """A bound on the CURRENT stage — never a delivery time.

    FR-84's "range with its basis, never a false precision". The system can
    bound the stages a Temporal timer governs and nothing else: it has no
    model of how long cooking takes and none of how long a road takes. So
    this says when the stage it names must end, one way or the other
    (including by cancellation), and says which timer it got that from.

    `low_s` is genuinely 0 for most stages: a restaurant may accept this
    second. Quoting a non-zero floor would invent patience the data does
    not support.
    """

    low_s: int
    high_s: int
    basis: str
    covers: str


@dataclass(frozen=True)
class Verdict:
    """What the renderer is allowed to say, and nothing more.

    `facts` is the whole permitted vocabulary for a template or a prompt: a
    renderer that needs something absent from here must come back and ask
    the resolver for it, rather than reaching into an order row and
    deciding for itself.
    """

    reason: ReasonCode
    overdue: bool | None
    stage: str
    stage_elapsed_s: int | None
    eta: Eta | None = None
    facts: Mapping[str, object] = field(default_factory=dict[str, object])


def _elapsed(since: datetime | None, now: datetime) -> int | None:
    """Whole seconds, floored at zero.

    Clock skew between the order service and this one can make a milestone
    look like it is in the future. A negative age would render as "ready
    -3 seconds ago"; zero is the honest floor.
    """
    if since is None:
        return None
    return max(0, int((now - since).total_seconds()))


def _bounded(
    elapsed: int | None, budget_s: int, *, basis: str, covers: str
) -> tuple[bool | None, Eta | None]:
    """(overdue, eta) for a stage a timer governs.

    Without a start moment there is no age and therefore no judgement —
    `(None, None)`, which is how a pre-ADR-0046 order with null milestones
    flows through here without pretending to be on time.
    """
    if elapsed is None:
        return None, None
    remaining = budget_s - elapsed
    if remaining <= 0:
        # Past the deadline. The timer is the thing that acts next, and
        # saying "0 to 0 minutes" would read as an estimate rather than as
        # the overrun it is.
        return True, None
    return False, Eta(low_s=0, high_s=remaining, basis=f"{basis}={budget_s}s", covers=covers)


def resolve(
    timeline: Timeline,
    *,
    now: datetime,
    delivery: Delivery | None = None,
    load: KitchenLoad | None = None,
    budget: Budget | None = None,
) -> Verdict:
    """The one judgement in this milestone.

    Branches on the ORDER's status, because the order row is the system of
    record for an order. Dispatch refines exactly one stage (READY, where
    the question is whether a courier exists yet) and kitchen load refines
    exactly one more (the kitchen, where the question is whether congestion
    explains the wait). Nothing else is allowed to override the status: a
    dispatch row claiming DELIVERED for an order the order service still
    calls READY is a propagation lag, and answering from the laggard would
    tell a customer their food arrived when it has not.
    """
    budget = budget or Budget()
    status = timeline.status

    if status in ("DELIVERED", "SETTLED"):
        return Verdict(
            reason=ReasonCode.DELIVERED,
            overdue=None,
            stage="delivered",
            stage_elapsed_s=_elapsed(timeline.picked_up_at, now),
        )

    if status == "REFUNDED":
        return Verdict(
            reason=ReasonCode.REFUNDED,
            overdue=None,
            stage="refunded",
            stage_elapsed_s=None,
            facts={"cancel_reason": timeline.cancel_reason},
        )

    if status in ("CANCELLING", "CANCELLED"):
        reason = _BY_CANCEL_REASON.get(timeline.cancel_reason or "", ReasonCode.UNKNOWN)
        return Verdict(
            reason=reason,
            overdue=None,
            stage="cancelled",
            stage_elapsed_s=None,
            facts={
                # The kitchen had already cooked when a no-courier cancel
                # lands (FR-32) — the one cancellation born after the food
                # exists, and the renderer owes the customer that.
                "reached_kitchen": timeline.accepted_at is not None,
                "payment_held": _payment_held(timeline),
                # CANCELLING is the unwind IN PROGRESS: the status moves
                # first and the void runs after it. The past tense "your
                # hold has been released" is a lie for however long that
                # takes, and under a degraded PSP the compensation retries
                # for a long time.
                "unwind_done": status == "CANCELLED",
            },
        )

    if status == "PICKED_UP":
        return Verdict(
            reason=ReasonCode.IN_TRANSIT,
            # No road-time model exists, so there is no deadline to be past.
            overdue=None,
            stage="in_transit",
            stage_elapsed_s=_elapsed(timeline.picked_up_at, now),
        )

    if status == "READY":
        return _ready(timeline, now, delivery, budget)

    if status in ("ACCEPTED", "PREPARING"):
        return _kitchen(timeline, now, load)

    if status == "CONFIRMED":
        overdue, eta = _bounded(
            _elapsed(timeline.confirmed_at, now),
            budget.accept_timeout_s,
            basis="accept_timeout_s",
            covers="until the restaurant accepts or the order is cancelled",
        )
        return Verdict(
            reason=ReasonCode.AWAITING_RESTAURANT,
            overdue=overdue,
            stage="awaiting_restaurant",
            stage_elapsed_s=_elapsed(timeline.confirmed_at, now),
            eta=eta,
            # The overdue copy predicts a cancellation, so it owes the same
            # money sentence a cancellation does. CONFIRMED is downstream of
            # a successful authorization, so this is all but always True —
            # but it is read, not assumed.
            facts={"payment_held": _payment_held(timeline), "unwind_done": False},
        )

    if status in ("PLACED", "VALIDATED", "PAYMENT_CLEARED"):
        overdue, eta = _bounded(
            _elapsed(timeline.placed_at, now),
            budget.forward_deadline_s,
            basis="forward_deadline_s",
            covers="until the order reaches the restaurant or is cancelled",
        )
        return Verdict(
            reason=ReasonCode.PAYMENT_IN_PROGRESS,
            overdue=overdue,
            stage="payment_in_progress",
            stage_elapsed_s=_elapsed(timeline.placed_at, now),
            eta=eta,
            facts={"payment_held": _payment_held(timeline), "unwind_done": False},
        )

    # A status this module has never heard of. Part A may add one; an
    # explanation engine that guessed would be wrong exactly when the
    # system is doing something new.
    return Verdict(
        reason=ReasonCode.UNKNOWN,
        overdue=None,
        stage="unknown",
        stage_elapsed_s=None,
        facts={"status": status},
    )


def _ready(timeline: Timeline, now: datetime, delivery: Delivery | None, budget: Budget) -> Verdict:
    """READY splits on whether a courier exists yet — the only stage where
    dispatch changes the answer, and the stage where FR-32's cancellation
    deadline is running."""
    waiting = _elapsed(timeline.ready_at, now)

    if delivery is not None and delivery.state == "PICKED_UP":
        # Dispatch says the courier HAS the food while the order row has
        # not caught up (MARK_PICKED_UP still retrying). Saying "hasn't
        # collected it yet" would contradict a fact we just read, so this
        # reports the collection and leaves the timing to the order row.
        return Verdict(
            reason=ReasonCode.COURIER_ON_THE_WAY,
            overdue=None,
            stage="courier_to_pickup",
            stage_elapsed_s=_elapsed(delivery.assigned_at, now),
            facts={"collected": True, "food_ready_for_s": waiting},
        )

    if delivery is not None and delivery.state == "ASSIGNED":
        # A courier is coming to collect. `pickup_timeout_s` runs from the
        # assignment, which dispatch owns — without it there is an elapsed
        # time and no deadline, and the renderer says so rather than
        # borrowing `ready_at` and blaming the courier for the wait before
        # they were even offered the job.
        overdue, eta = _bounded(
            _elapsed(delivery.assigned_at, now),
            budget.pickup_timeout_s,
            basis="pickup_timeout_s",
            covers="until the courier collects the order",
        )
        return Verdict(
            reason=ReasonCode.COURIER_ON_THE_WAY,
            overdue=overdue,
            stage="courier_to_pickup",
            stage_elapsed_s=_elapsed(delivery.assigned_at, now),
            eta=eta,
            facts={"food_ready_for_s": waiting},
        )

    overdue, eta = _bounded(
        waiting,
        budget.no_rider_deadline_s,
        basis="no_rider_deadline_s",
        covers="until a courier is assigned or the order is cancelled",
    )
    return Verdict(
        reason=ReasonCode.AWAITING_COURIER,
        overdue=overdue,
        stage="awaiting_courier",
        stage_elapsed_s=waiting,
        eta=eta,
        facts={
            # The food is cooked and sitting. That is the fact the customer
            # most needs and the one an order status alone never conveys.
            "food_is_ready": True,
            # READY is downstream of CONFIRMED, so the overdue copy — which
            # predicts FR-32's cancellation — must not tell this customer
            # nothing was charged. They are the ONE whose meal was made.
            "payment_held": _payment_held(timeline),
            "unwind_done": False,
        },
    )


def _kitchen(timeline: Timeline, now: datetime, load: KitchenLoad | None) -> Verdict:
    """The kitchen has no timer, so `overdue` is None here in every branch.

    Congestion is the one judgement load supports, and the bar is the same
    one the reservation gate uses: `active >= capacity` is the state in
    which this kitchen cannot take another order. Anything softer would be
    a threshold invented here to make the prose more interesting.

    `load is None` means nobody observed a congestion number — an unknown
    kitchen or an unreachable Inventory (FR-85). It must not read as "not
    busy", so it falls through to the plain preparing answer, which claims
    nothing about why.
    """
    # The STATUS decides which stage this is; the stamp only decides
    # whether we can put a clock on it. Reading the stamp alone told a
    # PREPARING order with no `preparing_at` — every row placed before
    # migration 0012, which is never backfilled — that the restaurant
    # "still hasn't started cooking", and then invited the customer to
    # cancel food that was on the pass.
    cooking = timeline.status == "PREPARING"
    started = timeline.preparing_at
    stage = "preparing" if cooking else "kitchen_not_started"
    since = started if cooking else timeline.accepted_at
    elapsed = _elapsed(since, now)

    if load is not None and load.active >= load.capacity:
        return Verdict(
            reason=ReasonCode.KITCHEN_BUSY,
            overdue=None,
            stage=stage,
            stage_elapsed_s=elapsed,
            facts={"active": load.active, "capacity": load.capacity, "as_of": load.as_of},
        )

    reason = ReasonCode.KITCHEN_PREPARING if cooking else ReasonCode.KITCHEN_NOT_STARTED
    return Verdict(reason=reason, overdue=None, stage=stage, stage_elapsed_s=elapsed)
