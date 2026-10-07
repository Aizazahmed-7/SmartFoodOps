"""The reason resolver, over every branch it has (FR-82, FR-84).

FR-82 asks for exhaustive branch coverage, so the last test in this file
asserts that every `ReasonCode` member is actually produced by something
above it. That is the test that keeps this honest: adding a code without
a path to it, or losing a path in a refactor, fails here rather than in a
customer's answer.
"""

from datetime import UTC, datetime, timedelta

import pytest
from ai_assistant.domain.explain import (
    Budget,
    Delivery,
    ReasonCode,
    Timeline,
    Verdict,
    resolve,
)
from ai_assistant.domain.ports import KitchenLoad

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PLACED = NOW - timedelta(minutes=30)


def _resolve(timeline: Timeline, **kwargs) -> Verdict:
    return resolve(timeline, now=NOW, **kwargs)


def _at(status: str, **stamps) -> Timeline:
    return Timeline(status=status, placed_at=PLACED, **stamps)


def _ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


# ── the saga, before the restaurant sees anything ──────────────────


@pytest.mark.parametrize("status", ["PLACED", "VALIDATED", "PAYMENT_CLEARED"])
def test_the_pre_kitchen_saga_is_bounded_by_the_forward_deadline(status):
    verdict = _resolve(Timeline(status=status, placed_at=_ago(seconds=60)))
    assert verdict.reason is ReasonCode.PAYMENT_IN_PROGRESS
    assert verdict.overdue is False
    assert verdict.eta is not None
    assert verdict.eta.high_s == 240  # 300 - 60
    assert verdict.eta.basis == "forward_deadline_s=300s"


# ── the restaurant's decision window ───────────────────────────────


def test_awaiting_restaurant_counts_from_confirmed_not_placed():
    """The whole reason `confirmed_at` exists. Timing this window from
    `placed_at` would charge the restaurant for the saga's reserve-and-
    authorize round trip — here, 28 of the 30 minutes."""
    verdict = _resolve(_at("CONFIRMED", confirmed_at=_ago(seconds=60)))
    assert verdict.reason is ReasonCode.AWAITING_RESTAURANT
    assert verdict.stage_elapsed_s == 60
    assert verdict.eta is not None and verdict.eta.high_s == 120  # 180 - 60


def test_a_restaurant_past_its_window_is_overdue_with_no_estimate():
    """Past the deadline the timer is the thing that acts next. Quoting
    "0 to 0 minutes" would read as an estimate rather than an overrun."""
    verdict = _resolve(_at("CONFIRMED", confirmed_at=_ago(seconds=200)))
    assert verdict.overdue is True
    assert verdict.eta is None


def test_a_confirmed_order_with_no_stamp_is_judged_by_nothing():
    """Rows predating ADR-0046 have null milestones. No start moment means
    no age and no verdict on lateness — never a cheerful "on time"."""
    verdict = _resolve(_at("CONFIRMED"))
    assert verdict.reason is ReasonCode.AWAITING_RESTAURANT
    assert verdict.overdue is None
    assert verdict.stage_elapsed_s is None and verdict.eta is None


def test_an_operators_widened_budget_changes_what_late_means():
    late = _at("CONFIRMED", confirmed_at=_ago(seconds=200))
    assert _resolve(late).overdue is True
    assert _resolve(late, budget=Budget(accept_timeout_s=600)).overdue is False


# ── the kitchen, which has no timer ────────────────────────────────


def test_accepted_but_not_started_is_its_own_answer():
    verdict = _resolve(_at("ACCEPTED", accepted_at=_ago(minutes=4)))
    assert verdict.reason is ReasonCode.KITCHEN_NOT_STARTED
    assert verdict.stage_elapsed_s == 240
    assert verdict.overdue is None  # no cooking budget exists to be past


def test_preparing_reports_elapsed_and_claims_nothing_about_why():
    verdict = _resolve(
        _at("PREPARING", accepted_at=_ago(minutes=20), preparing_at=_ago(minutes=18))
    )
    assert verdict.reason is ReasonCode.KITCHEN_PREPARING
    assert verdict.stage_elapsed_s == 18 * 60
    assert verdict.overdue is None


def test_a_saturated_kitchen_is_the_one_judgement_load_supports():
    """The bar is the reservation gate's own: `active >= capacity` is the
    state in which this kitchen cannot take another order."""
    load = KitchenLoad(restaurant_id="rst_1", active=8, capacity=8, as_of=NOW)
    verdict = _resolve(_at("PREPARING", preparing_at=_ago(minutes=18)), load=load)
    assert verdict.reason is ReasonCode.KITCHEN_BUSY
    assert verdict.facts["active"] == 8 and verdict.facts["capacity"] == 8


def test_capacity_lowered_under_a_running_kitchen_still_reads_as_busy():
    load = KitchenLoad(restaurant_id="rst_1", active=5, capacity=2, as_of=NOW)
    assert _resolve(_at("PREPARING", preparing_at=_ago(minutes=5)), load=load).reason is (
        ReasonCode.KITCHEN_BUSY
    )


def test_a_kitchen_with_room_is_not_blamed_for_the_wait():
    load = KitchenLoad(restaurant_id="rst_1", active=1, capacity=8, as_of=NOW)
    assert _resolve(_at("PREPARING", preparing_at=_ago(minutes=25)), load=load).reason is (
        ReasonCode.KITCHEN_PREPARING
    )


def test_an_unobservable_kitchen_does_not_read_as_quiet():
    """FR-85's None means nobody observed a congestion number — an unknown
    kitchen or an unreachable Inventory. It must claim nothing either way."""
    assert _resolve(_at("PREPARING", preparing_at=_ago(minutes=25)), load=None).reason is (
        ReasonCode.KITCHEN_PREPARING
    )


# ── READY: the only stage dispatch changes ─────────────────────────


def test_ready_with_no_courier_says_the_food_is_cooked():
    """The fact an order status alone never conveys."""
    verdict = _resolve(_at("READY", ready_at=_ago(minutes=4)))
    assert verdict.reason is ReasonCode.AWAITING_COURIER
    assert verdict.facts["food_is_ready"] is True
    assert verdict.eta is not None and verdict.eta.high_s == 600 - 240


def test_ready_past_the_no_rider_deadline_is_overdue():
    verdict = _resolve(_at("READY", ready_at=_ago(minutes=11)))
    assert verdict.overdue is True and verdict.eta is None


def test_an_offering_delivery_is_still_awaiting_a_courier():
    """OFFERING means the cascade is running, not that anyone accepted."""
    verdict = _resolve(_at("READY", ready_at=_ago(minutes=2)), delivery=Delivery(state="OFFERING"))
    assert verdict.reason is ReasonCode.AWAITING_COURIER


def test_an_assigned_courier_is_timed_from_the_assignment():
    """`pickup_timeout_s` runs from the assignment. Borrowing `ready_at`
    would blame the courier for the wait before they were offered the job
    — here, the 8 minutes the order spent looking for anyone."""
    verdict = _resolve(
        _at("READY", ready_at=_ago(minutes=10)),
        delivery=Delivery(state="ASSIGNED", assigned_at=_ago(minutes=2)),
    )
    assert verdict.reason is ReasonCode.COURIER_ON_THE_WAY
    assert verdict.stage_elapsed_s == 120
    assert verdict.eta is not None and verdict.eta.high_s == 300 - 120
    assert verdict.facts["food_ready_for_s"] == 600


def test_an_assignment_with_no_timestamp_has_an_age_of_nothing():
    verdict = _resolve(_at("READY", ready_at=_ago(minutes=3)), delivery=Delivery(state="ASSIGNED"))
    assert verdict.reason is ReasonCode.COURIER_ON_THE_WAY
    assert verdict.overdue is None and verdict.eta is None


def test_a_dispatch_row_ahead_of_the_order_never_overrides_it():
    """Dispatch saying DELIVERED while the order service still says READY
    is propagation lag. Answering from the laggard would tell a customer
    their food arrived when it has not."""
    verdict = _resolve(_at("READY", ready_at=_ago(minutes=1)), delivery=Delivery(state="DELIVERED"))
    assert verdict.reason is ReasonCode.AWAITING_COURIER


# ── on the road, and finished ──────────────────────────────────────


def test_in_transit_has_an_age_and_deliberately_no_deadline():
    """OSRM ETAs stay deferred, so there is no road-time model and nothing
    to be late against."""
    verdict = _resolve(_at("PICKED_UP", picked_up_at=_ago(minutes=6)))
    assert verdict.reason is ReasonCode.IN_TRANSIT
    assert verdict.stage_elapsed_s == 360
    assert verdict.overdue is None and verdict.eta is None


@pytest.mark.parametrize("status", ["DELIVERED", "SETTLED"])
def test_both_ends_of_a_finished_order_read_as_delivered(status):
    """SETTLED is DELIVERED plus money. To a customer asking where their
    food is, they are the same answer."""
    assert _resolve(_at(status, picked_up_at=_ago(minutes=20))).reason is ReasonCode.DELIVERED


def test_a_refunded_order_is_its_own_code():
    assert _resolve(_at("REFUNDED", cancel_reason="customer_cancelled")).reason is (
        ReasonCode.REFUNDED
    )


# ── cancellations, by cause (FR-86 renders these) ──────────────────


@pytest.mark.parametrize(
    ("cancel_reason", "expected"),
    [
        ("customer_cancelled", ReasonCode.CANCELLED_BY_CUSTOMER),
        ("restaurant_rejected", ReasonCode.CANCELLED_BY_RESTAURANT),
        ("restaurant_timeout", ReasonCode.CANCELLED_RESTAURANT_SILENT),
        ("item_unavailable", ReasonCode.CANCELLED_ITEM_UNAVAILABLE),
        ("at_capacity", ReasonCode.CANCELLED_KITCHEN_FULL),
        ("payment_declined", ReasonCode.CANCELLED_PAYMENT_DECLINED),
        ("no_rider_available", ReasonCode.CANCELLED_NO_COURIER),
        ("system_timeout", ReasonCode.CANCELLED_SYSTEM),
    ],
)
@pytest.mark.parametrize("status", ["CANCELLED", "CANCELLING"])
def test_every_cancel_reason_order_can_write_has_a_code(status, cancel_reason, expected):
    assert _resolve(_at(status, cancel_reason=cancel_reason)).reason is expected


def test_a_no_courier_cancel_records_that_the_food_was_cooked():
    """FR-32's cancellation is the one born after the kitchen cooked, and
    the renderer owes the customer that distinction."""
    cooked = _resolve(
        _at(
            "CANCELLED",
            cancel_reason="no_rider_available",
            accepted_at=_ago(minutes=25),
            ready_at=_ago(minutes=12),
        )
    )
    never = _resolve(_at("CANCELLED", cancel_reason="payment_declined"))
    assert cooked.facts["reached_kitchen"] is True
    assert never.facts["reached_kitchen"] is False


def test_a_cancel_reason_this_module_has_never_heard_of_is_unknown():
    """Order may add a reason. An engine that guessed would be wrong
    exactly when the system does something new (UC-22: hand off, never
    guess)."""
    assert _resolve(_at("CANCELLED", cancel_reason="kitchen_fire")).reason is ReasonCode.UNKNOWN
    assert _resolve(_at("CANCELLED")).reason is ReasonCode.UNKNOWN


def test_a_status_this_module_has_never_heard_of_is_unknown():
    verdict = _resolve(_at("TELEPORTING"))
    assert verdict.reason is ReasonCode.UNKNOWN
    assert verdict.facts["status"] == "TELEPORTING"


# ── clocks ─────────────────────────────────────────────────────────


def test_a_milestone_in_the_future_ages_to_zero_not_below():
    """Skew between the order service's clock and ours must not render as
    "ready -3 seconds ago"."""
    verdict = _resolve(_at("READY", ready_at=NOW + timedelta(seconds=3)))
    assert verdict.stage_elapsed_s == 0
    assert verdict.overdue is False


# ── the exhaustiveness check ───────────────────────────────────────


def _witnesses() -> dict[ReasonCode, Verdict]:
    """One resolution per code, driven here rather than accumulated as a
    side effect of the tests above — so this proves the same thing whether
    the whole file runs or only this test does."""
    cases = [
        _resolve(_at("PLACED")),
        _resolve(_at("CONFIRMED", confirmed_at=_ago(seconds=10))),
        _resolve(_at("ACCEPTED", accepted_at=_ago(minutes=2))),
        _resolve(_at("PREPARING", preparing_at=_ago(minutes=5))),
        _resolve(
            _at("PREPARING", preparing_at=_ago(minutes=5)),
            load=KitchenLoad(restaurant_id="rst_1", active=8, capacity=8, as_of=NOW),
        ),
        _resolve(_at("READY", ready_at=_ago(minutes=2))),
        _resolve(
            _at("READY", ready_at=_ago(minutes=2)),
            delivery=Delivery(state="ASSIGNED", assigned_at=_ago(minutes=1)),
        ),
        _resolve(_at("PICKED_UP", picked_up_at=_ago(minutes=3))),
        _resolve(_at("DELIVERED", picked_up_at=_ago(minutes=9))),
        _resolve(_at("REFUNDED")),
        *(
            _resolve(_at("CANCELLED", cancel_reason=reason))
            for reason in (
                "customer_cancelled",
                "restaurant_rejected",
                "restaurant_timeout",
                "item_unavailable",
                "at_capacity",
                "payment_declined",
                "no_rider_available",
                "system_timeout",
            )
        ),
        _resolve(_at("TELEPORTING")),
    ]
    return {verdict.reason: verdict for verdict in cases}


def test_every_reason_code_is_reachable():
    """FR-82's "closed set, exhaustively tested", enforced rather than
    asserted in a docstring. A code with no path to it is either dead or a
    template nobody can trigger; a path lost in a refactor is a customer
    getting UNKNOWN where an answer existed."""
    produced = set(_witnesses())
    assert produced == set(ReasonCode), f"never produced: {sorted(set(ReasonCode) - produced)}"


def test_no_verdict_claims_a_deadline_it_cannot_justify():
    """An ETA is only ever a remaining budget, so a bound must be positive
    and must name the timer it came from — FR-84's "with its basis"."""
    for reason, verdict in _witnesses().items():
        if verdict.eta is None:
            continue
        assert verdict.eta.high_s > verdict.eta.low_s, reason
        assert verdict.eta.basis.endswith("s") and "=" in verdict.eta.basis, reason
        assert verdict.eta.covers, reason
        # A stage with an estimate is by definition not yet past its
        # deadline; the two must never disagree.
        assert verdict.overdue is False, reason


# ── degraded rows: the status is the system of record ──────────────


def test_a_preparing_order_with_no_stamp_is_still_preparing():
    """Migration 0012 is never backfilled, so an order in PREPARING placed
    before it has `preparing_at = None`. Reading the stamp instead of the
    status told that customer the restaurant "still hasn't started
    cooking" — and invited them to cancel food that was on the pass."""
    verdict = _resolve(_at("PREPARING", accepted_at=_ago(minutes=20)))
    assert verdict.reason is ReasonCode.KITCHEN_PREPARING
    assert verdict.stage_elapsed_s is None  # no clock, and none claimed


def test_an_accepted_order_is_not_reported_as_cooking():
    verdict = _resolve(_at("ACCEPTED", accepted_at=_ago(minutes=3)))
    assert verdict.reason is ReasonCode.KITCHEN_NOT_STARTED


def test_dispatch_reporting_a_collection_is_not_called_uncollected():
    """The order row lags (MARK_PICKED_UP retrying) while dispatch already
    says PICKED_UP. Claiming the courier "hasn't collected it yet" would
    contradict a fact the resolver just read."""
    verdict = _resolve(
        _at("READY", ready_at=_ago(minutes=12)),
        delivery=Delivery(state="PICKED_UP", assigned_at=_ago(minutes=9)),
    )
    assert verdict.reason is ReasonCode.COURIER_ON_THE_WAY
    assert verdict.facts["collected"] is True
    assert verdict.overdue is None  # nothing to be late for; it is collected
