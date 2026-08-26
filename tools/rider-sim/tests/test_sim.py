"""The sim's brains: motion arithmetic + the tick decision table."""

import random

import pytest
from rider_sim.main import ARRIVE_M, Plan, SimRider
from rider_sim.motion import meters_between, step_toward

HOME = (39.8025, -89.6478)
SHOP = (39.7912, -89.6644)


def test_meters_between_is_sane():
    assert meters_between(HOME, HOME) == 0.0
    assert meters_between(SHOP, HOME) == pytest.approx(1890, rel=0.05)


def test_step_toward_glides_and_lands_exactly():
    moved = step_toward(SHOP, HOME, speed_mps=12, dt_s=1.0)
    assert 0 < meters_between(SHOP, moved) <= 12.001
    # Within reach → lands ON the target, never orbits it.
    near = step_toward(HOME, HOME, speed_mps=12, dt_s=1.0)
    assert near == HOME
    close = (HOME[0] + 0.00001, HOME[1])
    assert step_toward(close, HOME, speed_mps=12, dt_s=1.0) == HOME


def _rider(**kwargs) -> SimRider:
    return SimRider("r@demo", SHOP, rng=random.Random(7), **kwargs)


def test_idle_rider_does_nothing():
    assert _rider().decide({"offer": None, "delivery": None}, dt_s=1.0) == Plan()


def test_offer_is_accepted_at_full_rate_and_ghosted_at_zero():
    me = {"offer": {"offer_id": "off_1", "order_id": "ord_1"}, "delivery": None}
    assert _rider().decide(me, dt_s=1.0).accept == ("off_1", "ord_1")
    ghost = _rider(accept_rate=0.0).decide(me, dt_s=1.0)
    assert ghost == Plan()  # the cascade's 15s window is the answer


def test_assigned_rider_glides_to_pickup_then_taps():
    delivery = {
        "order_id": "ord_1",
        "state": "ASSIGNED",
        "pickup": {"lat": SHOP[0], "lon": SHOP[1]},
        "dropoff": {"lat": HOME[0], "lon": HOME[1]},
    }
    far = SimRider("r@demo", HOME)
    plan = far.decide({"delivery": delivery}, dt_s=1.0)
    assert plan.move_to is not None and plan.tap is None
    assert meters_between(HOME, plan.move_to) <= 12.001
    at_shop = SimRider("r@demo", SHOP)
    assert at_shop.decide({"delivery": delivery}, dt_s=1.0).tap == ("pickup", "ord_1")


def test_picked_up_rider_heads_home_and_delivers():
    delivery = {
        "order_id": "ord_1",
        "state": "PICKED_UP",
        "pickup": {"lat": SHOP[0], "lon": SHOP[1]},
        "dropoff": {"lat": HOME[0], "lon": HOME[1]},
    }
    plan = SimRider("r@demo", HOME).decide({"delivery": delivery}, dt_s=1.0)
    assert plan.tap == ("deliver", "ord_1") and plan.done is True
    assert ARRIVE_M == 40.0  # the tap radius the FE's map mirrors
