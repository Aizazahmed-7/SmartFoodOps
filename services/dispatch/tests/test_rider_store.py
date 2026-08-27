"""The DDB stores against moto — including THE DRILL: ADR-0011's chaos
case (concurrent offers to one rider yield exactly one reservation) run
with real threads against real condition evaluation."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from botocore.exceptions import ClientError
from dispatch.adapters.rider_store import ensure_tables

PICKUP = (39.7912, -89.6644)
DROPOFF = (39.8025, -89.6478)


def _offering(deliveries, order_id="ord_1", rider_id="r1", offer_id="off_1", attempt=1):
    return deliveries.put_offering(
        order_id,
        rider_id=rider_id,
        offer_id=offer_id,
        user_id="usr_1",
        restaurant_name="Biryani House",
        pickup=PICKUP,
        dropoff=DROPOFF,
        attempt=attempt,
    )


# ── the lock (rider_state) ─────────────────────────────────────────


def test_reserve_requires_an_online_rider(riders):
    assert riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1) is False  # absent
    riders.set_online("r1")
    assert riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1) is True
    state = riders.get("r1")
    assert state["offer_lock"]["offer_id"] == "off_1"
    assert state["offers_made"] == 1


def test_offline_rider_is_never_reserved(riders):
    riders.set_online("r1")
    riders.set_offline("r1")
    assert riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1) is False


def test_THE_DRILL_concurrent_offers_yield_exactly_one_lock(riders):
    """Eight threads race the same rider with distinct offers. DDB's
    per-item conditional write is the only referee — exactly one wins.
    The barrier holds every racer at the line until all eight are in
    flight, so the pile-up is real on every run, not just on slow ones."""
    riders.set_online("r1")
    start = threading.Barrier(8)

    def racer(i: int) -> bool:
        start.wait(timeout=10)
        return riders.reserve("r1", offer_id=f"off_{i}", order_id=f"ord_{i}", cap=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        wins = list(pool.map(racer, range(8)))
    assert wins.count(True) == 1
    assert riders.get("r1")["offers_made"] == 1  # only the winner's write landed


def test_accept_converts_lock_to_active_delivery(riders):
    riders.set_online("r1")
    riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1)
    assert riders.accept("r1", offer_id="off_1", order_id="ord_1") is True
    state = riders.get("r1")
    assert "offer_lock" not in state
    assert state["active_deliveries"] == ["ord_1"]
    assert state["offers_accepted"] == 1


def test_late_accept_after_release_noops(riders):
    """The 15s timer fired and released; the rider's tap arrives late.
    The conditional answers False and nothing half-applies."""
    riders.set_online("r1")
    riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1)
    assert riders.release_offer("r1", offer_id="off_1") is True
    assert riders.accept("r1", offer_id="off_1", order_id="ord_1") is False
    assert "active_deliveries" not in riders.get("r1")


def test_accept_vs_release_race_has_exactly_one_winner(riders):
    """The other direction of the drill: expiry and accept race the SAME
    lock — both are conditioned on it, so exactly one write survives.
    Same barrier discipline: both taps leave the line together."""
    riders.set_online("r1")
    riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1)
    start = threading.Barrier(2)

    def tap() -> bool:
        start.wait(timeout=10)
        return riders.accept("r1", offer_id="off_1", order_id="ord_1")

    def expire() -> bool:
        start.wait(timeout=10)
        return riders.release_offer("r1", offer_id="off_1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        accept = pool.submit(tap)
        release = pool.submit(expire)
    assert [accept.result(), release.result()].count(True) == 1


def test_capacity_blocks_a_second_delivery_and_frees_after_finish(riders):
    riders.set_online("r1")
    riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1)
    riders.accept("r1", offer_id="off_1", order_id="ord_1")
    # At cap: a fresh reserve fails on size(active_deliveries) < :cap.
    assert riders.reserve("r1", offer_id="off_2", order_id="ord_2", cap=1) is False
    assert riders.finish_delivery("r1", order_id="ord_1") is True
    assert riders.finish_delivery("r1", order_id="ord_1") is False  # idempotent replay
    # The emptied SET vanished as an attribute — the OR-absent guard is
    # what keeps an idle rider reservable (the canonical-expression note).
    assert riders.reserve("r1", offer_id="off_2", order_id="ord_2", cap=1) is True


def test_release_of_a_superseded_offer_noops(riders):
    riders.set_online("r1")
    riders.reserve("r1", offer_id="off_1", order_id="ord_1", cap=1)
    assert riders.release_offer("r1", offer_id="off_9") is False  # not this offer's lock
    assert riders.get("r1")["offer_lock"]["offer_id"] == "off_1"


def test_non_conditional_ddb_errors_stay_loud(riders):
    with pytest.raises(ClientError):
        riders._c.update_item(  # a genuinely broken call, not a lost race
            TableName="nope", Key={"rider_id": {"S": "r1"}}, UpdateExpression="SET x = :x"
        )


# ── the delivery state machine ─────────────────────────────────────


def test_offering_then_assign_then_pickup_then_deliver(deliveries):
    assert _offering(deliveries) is True
    assert deliveries.assign("ord_1", offer_id="off_1") is True
    assert deliveries.mark_picked_up("ord_1", rider_id="r1") is True
    assert deliveries.mark_delivered("ord_1", rider_id="r1") is True
    row = deliveries.get("ord_1")
    assert row["state"] == "DELIVERED"
    assert row["pickup_lat"] == pytest.approx(PICKUP[0])
    for stamp in ("offered_at", "assigned_at", "picked_up_at", "delivered_at"):
        assert stamp in row


def test_reoffer_may_overwrite_offering_but_never_assigned(deliveries):
    _offering(deliveries)
    assert _offering(deliveries, rider_id="r2", offer_id="off_2", attempt=2) is True  # cascade
    deliveries.assign("ord_1", offer_id="off_2")
    assert _offering(deliveries, rider_id="r3", offer_id="off_3", attempt=3) is False


def test_assign_requires_the_matching_live_offer(deliveries):
    _offering(deliveries)
    assert deliveries.assign("ord_1", offer_id="off_stale") is False
    assert deliveries.get("ord_1")["state"] == "OFFERING"


def test_wrong_rider_cannot_drive_the_delivery(deliveries):
    _offering(deliveries)
    deliveries.assign("ord_1", offer_id="off_1")
    assert deliveries.mark_picked_up("ord_1", rider_id="r9") is False
    assert deliveries.mark_delivered("ord_1", rider_id="r1") is False  # not picked up yet


def test_unassign_loses_to_a_completed_pickup(deliveries):
    """ADR-0011's revoke rule: a rider who already scanned pickup wins."""
    _offering(deliveries)
    deliveries.assign("ord_1", offer_id="off_1")
    deliveries.mark_picked_up("ord_1", rider_id="r1")
    assert deliveries.unassign("ord_1", rider_id="r1") is False


def test_unassign_reverts_a_stalled_assignment(deliveries):
    _offering(deliveries)
    deliveries.assign("ord_1", offer_id="off_1")
    assert deliveries.unassign("ord_1", rider_id="r1") is True
    assert deliveries.get("ord_1")["state"] == "OFFERING"


def test_cancel_covers_both_prepickup_states_and_refuses_later(deliveries):
    _offering(deliveries)
    assert deliveries.cancel("ord_1") is True  # from OFFERING
    _offering(deliveries, order_id="ord_2", offer_id="off_2")
    deliveries.assign("ord_2", offer_id="off_2")
    assert deliveries.cancel("ord_2") is True  # from ASSIGNED
    _offering(deliveries, order_id="ord_3", offer_id="off_3")
    deliveries.assign("ord_3", offer_id="off_3")
    deliveries.mark_picked_up("ord_3", rider_id="r1")
    assert deliveries.cancel("ord_3") is False  # FR-21: food is with the rider


def test_get_unknown_rows_answer_none(riders, deliveries):
    assert riders.get("ghost") is None
    assert deliveries.get("ord_ghost") is None


def test_ensure_tables_is_idempotent(ddb):
    ensure_tables(ddb, rider_state="rider_state", deliveries="deliveries")  # second run
    assert set(ddb.list_tables()["TableNames"]) == {"rider_state", "deliveries"}


def test_store_methods_reraise_non_conditional_errors(ddb):
    """A lost race returns False; a genuinely broken call (missing table)
    stays LOUD through every guarded method — never mistaken for a race."""
    from dispatch.adapters.rider_store import DeliveryStore, RiderStore

    riders = RiderStore(ddb, "no_such_table")
    deliveries = DeliveryStore(ddb, "no_such_table")
    for attempt in (
        lambda: riders.reserve("r1", offer_id="o", order_id="ord", cap=1),
        lambda: riders.accept("r1", offer_id="o", order_id="ord"),
        lambda: riders.release_offer("r1", offer_id="o"),
        lambda: riders.finish_delivery("r1", order_id="ord"),
        lambda: _offering(deliveries),
        lambda: deliveries.assign("ord_1", offer_id="o"),
    ):
        with pytest.raises(ClientError):
            attempt()
