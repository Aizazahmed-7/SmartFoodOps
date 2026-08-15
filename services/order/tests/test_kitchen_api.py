"""The kitchen surface end to end: feed (FIFO keyset, batched items,
claim scoping), the accept/reject decision matrix, preparing/ready
transitions with the food_ready signal contract, and saga-failure mapping.

Orders are seeded through the real placement API, then advanced through
the real transition() helper against the same FILE-backed sqlite the app
uses — the tests move state the same way the saga does, never by raw
UPDATE."""

import pytest
from fastapi.testclient import TestClient
from order.config import Settings
from order.domain.ports import SagaGone, SagaUnavailable
from order.main import create_app
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))
OWNER = headers_for(AuthContext(sub="usr_9", role="restaurant_admin", restaurant_id="rst_1"))
OTHER = headers_for(AuthContext(sub="usr_8", role="restaurant_admin", restaurant_id="rst_other"))
NO_CLAIM = headers_for(AuthContext(sub="usr_7", role="restaurant_admin"))

TO_CONFIRMED = [
    ("PLACED", "VALIDATED"),
    ("VALIDATED", "PAYMENT_CLEARED"),
    ("PAYMENT_CLEARED", "CONFIRMED"),
]
TO_ACCEPTED = [*TO_CONFIRMED, ("CONFIRMED", "ACCEPTED")]


@pytest.fixture()
def client(catalog, identity, saga, db_url, make_snapshot):
    catalog.snapshot = make_snapshot()
    settings = Settings(database_url=db_url, create_all=True, sweeper_interval_seconds=0)
    app = create_app(settings, catalog=catalog, identity=identity, saga=saga)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def place(place_order):
    # The feed assertions below expect qty=2 lines.
    def _place(client):
        return place_order(client, qty=2)

    return _place


@pytest.fixture()
def cancelled(advance_order):
    def _cancelled(db_url, order_id, reason, *, upto=TO_CONFIRMED):
        advance_order(
            db_url,
            order_id,
            [*upto, (upto[-1][1], "CANCELLING"), ("CANCELLING", "CANCELLED")],
            reason=reason,
        )

    return _cancelled


# ── the feed ───────────────────────────────────────────────────────


def test_feed_requires_a_valid_status(client):
    assert client.get("/v1/restaurant/orders", headers=OWNER).status_code == 422
    r = client.get("/v1/restaurant/orders", params={"status": "COOKING"}, headers=OWNER)
    assert r.status_code == 422


def test_feed_is_a_fifo_queue_with_batched_items(client, db_url, place, advance_order):
    ids = [place(client) for _ in range(3)]
    for order_id in ids:
        advance_order(db_url, order_id, TO_CONFIRMED)
    placed_only = place(client)  # stays PLACED — must not appear in this queue

    r = client.get("/v1/restaurant/orders", params={"status": "CONFIRMED"}, headers=OWNER)
    assert r.status_code == 200
    feed = r.json()
    assert {e["order_id"] for e in feed["items"]} == set(ids)
    assert placed_only not in {e["order_id"] for e in feed["items"]}
    keys = [(e["placed_at"], e["order_id"]) for e in feed["items"]]
    assert keys == sorted(keys)  # oldest first — a kitchen is a FIFO
    assert feed["next_cursor"] is None
    entry = feed["items"][0]
    assert entry["items"] == [{"menu_item_id": "itm_a", "name": "Chicken Biryani", "qty": 2}]
    assert entry["total_cents"] > 0 and entry["currency"] == "USD"


def test_feed_keyset_pagination_never_overlaps(client, db_url, place, advance_order):
    ids = {place(client) for _ in range(3)}
    for order_id in ids:
        advance_order(db_url, order_id, TO_CONFIRMED)

    first = client.get(
        "/v1/restaurant/orders", params={"status": "CONFIRMED", "limit": 2}, headers=OWNER
    ).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get(
        "/v1/restaurant/orders",
        params={"status": "CONFIRMED", "limit": 2, "cursor": first["next_cursor"]},
        headers=OWNER,
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    seen = {e["order_id"] for e in first["items"]} | {e["order_id"] for e in second["items"]}
    assert seen == ids


def test_feed_is_scoped_to_the_admins_claim(client, db_url, place, advance_order):
    advance_order(db_url, place(client), TO_CONFIRMED)
    r = client.get("/v1/restaurant/orders", params={"status": "CONFIRMED"}, headers=OTHER)
    assert r.json() == {"items": [], "next_cursor": None}
    assert (
        client.get(
            "/v1/restaurant/orders", params={"status": "CONFIRMED"}, headers=NO_CLAIM
        ).status_code
        == 404
    )


def test_feed_malformed_cursor_is_422(client):
    r = client.get(
        "/v1/restaurant/orders",
        params={"status": "CONFIRMED", "cursor": "not-a-cursor"},
        headers=OWNER,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_kitchen_surface_rejects_customers(client):
    r = client.get("/v1/restaurant/orders", params={"status": "CONFIRMED"}, headers=CUSTOMER)
    assert r.status_code == 403


# ── accept / reject: the decision matrix ───────────────────────────


def test_confirmed_decision_signals_the_saga_202(client, db_url, saga, place, advance_order):
    order_id = place(client)
    advance_order(db_url, order_id, TO_CONFIRMED)
    r = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=OWNER)
    assert r.status_code == 202
    assert r.json() == {"order_id": order_id, "decision": "accept", "status": "CONFIRMED"}
    assert saga.decisions == [(order_id, "accept")]

    r = client.post(f"/v1/restaurant/orders/{order_id}/reject", headers=OWNER)
    assert r.status_code == 202  # still CONFIRMED here: the workflow referees
    assert saga.decisions == [(order_id, "accept"), (order_id, "reject")]


def test_same_verdict_after_apply_is_200_without_resignalling(
    client, db_url, saga, place, advance_order, cancelled
):
    order_id = place(client)
    advance_order(db_url, order_id, TO_ACCEPTED)
    r = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=OWNER)
    assert r.status_code == 200
    assert r.json()["status"] == "ACCEPTED"
    assert saga.decisions == []  # DB truth answered; no signal

    rejected = place(client)
    cancelled(db_url, rejected, "restaurant_rejected")
    r = client.post(f"/v1/restaurant/orders/{rejected}/reject", headers=OWNER)
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"


def test_opposite_verdict_is_409_already_decided(client, db_url, place, advance_order, cancelled):
    accepted = place(client)
    advance_order(db_url, accepted, TO_ACCEPTED)
    r = client.post(f"/v1/restaurant/orders/{accepted}/reject", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"

    rejected = place(client)
    cancelled(db_url, rejected, "restaurant_rejected")
    r = client.post(f"/v1/restaurant/orders/{rejected}/accept", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"


def test_timer_cancelled_order_is_409_for_both_verdicts(client, db_url, place, cancelled):
    order_id = place(client)
    cancelled(db_url, order_id, "restaurant_timeout")
    for verdict in ("accept", "reject"):
        r = client.post(f"/v1/restaurant/orders/{order_id}/{verdict}", headers=OWNER)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"


def test_customer_cancelled_order_is_already_decided_not_state_conflict(
    client, db_url, place, advance_order, cancelled
):
    """S7: the customer can moot the fork — even after this kitchen's own
    accept was submitted. Its retry hears 'window closed', never 'your
    action does not fit' (which reads as a client bug)."""
    unwinding = place(client)  # mid-unwind AND terminal must both classify
    advance_order(
        db_url,
        unwinding,
        [*TO_CONFIRMED, ("CONFIRMED", "CANCELLING")],
        reason="customer_cancelled",
    )
    done = place(client)
    cancelled(db_url, done, "customer_cancelled")
    for order_id in (unwinding, done):
        for verdict in ("accept", "reject"):
            r = client.post(f"/v1/restaurant/orders/{order_id}/{verdict}", headers=OWNER)
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"
            assert "customer" in r.json()["error"]["message"]


def test_orders_that_died_before_the_kitchen_are_state_conflicts(client, db_url, place, cancelled):
    declined = place(client)
    cancelled(db_url, declined, "payment_declined", upto=[("PLACED", "VALIDATED")])
    r = client.post(f"/v1/restaurant/orders/{declined}/accept", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_STATE_CONFLICT"

    early = place(client)  # still PLACED — not in front of the kitchen yet
    r = client.post(f"/v1/restaurant/orders/{early}/accept", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_STATE_CONFLICT"
    assert r.json()["error"]["details"] == [{"field": "status", "issue": "order is PLACED"}]


def test_decisions_are_scoped_not_found_and_not_yours_alike(
    client, db_url, saga, place, advance_order
):
    order_id = place(client)
    advance_order(db_url, order_id, TO_CONFIRMED)
    for headers in (OTHER, NO_CLAIM):
        r = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=headers)
        assert r.status_code == 404
    assert client.post("/v1/restaurant/orders/ord_ghost/accept", headers=OWNER).status_code == 404
    assert saga.decisions == []


def test_decision_window_closing_underfoot_is_409(client, db_url, saga, place, advance_order):
    """DB said CONFIRMED, but the workflow finished before the signal
    landed (timer beat the click) — SagaGone maps to ALREADY_DECIDED."""
    order_id = place(client)
    advance_order(db_url, order_id, TO_CONFIRMED)
    saga.fail_with = SagaGone(f"ord::{order_id}")
    r = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"


def test_temporal_down_is_503_with_retry_after(client, db_url, saga, place, advance_order):
    order_id = place(client)
    advance_order(db_url, order_id, TO_CONFIRMED)
    saga.fail_with = SagaUnavailable("boom")
    r = client.post(f"/v1/restaurant/orders/{order_id}/accept", headers=OWNER)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["retry-after"] == "1"


# ── preparing / ready ──────────────────────────────────────────────


def test_preparing_moves_accepted_and_replays_200(client, db_url, place, advance_order):
    order_id = place(client)
    advance_order(db_url, order_id, TO_ACCEPTED)
    for _ in range(2):  # fresh apply, then transition()'s idempotent no-op
        r = client.post(f"/v1/restaurant/orders/{order_id}/preparing", headers=OWNER)
        assert r.status_code == 200
        assert r.json() == {"order_id": order_id, "status": "PREPARING"}


def test_preparing_before_accept_is_409(client, db_url, place, advance_order):
    order_id = place(client)
    advance_order(db_url, order_id, TO_CONFIRMED)
    r = client.post(f"/v1/restaurant/orders/{order_id}/preparing", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_STATE_CONFLICT"


def test_ready_signals_the_courier_and_resignals_on_replay(
    client, db_url, saga, place, advance_order
):
    order_id = place(client)
    advance_order(db_url, order_id, [*TO_ACCEPTED, ("ACCEPTED", "PREPARING")])
    r = client.post(f"/v1/restaurant/orders/{order_id}/ready", headers=OWNER)
    assert r.status_code == 200
    assert r.json() == {"order_id": order_id, "status": "READY"}
    assert saga.food_ready == [order_id]

    # Replay heals a moved-but-unsignalled crash: it signals AGAIN (the
    # DeliveryWorkflow's flag makes duplicates harmless).
    r = client.post(f"/v1/restaurant/orders/{order_id}/ready", headers=OWNER)
    assert r.status_code == 200
    assert saga.food_ready == [order_id, order_id]


def test_ready_skipping_preparing_is_409(client, db_url, saga, place, advance_order):
    order_id = place(client)
    advance_order(db_url, order_id, TO_ACCEPTED)
    r = client.post(f"/v1/restaurant/orders/{order_id}/ready", headers=OWNER)
    assert r.status_code == 409
    assert saga.food_ready == []  # refused BEFORE any signal


def test_ready_when_delivery_is_gone_still_answers_the_committed_truth(
    client, db_url, saga, place, advance_order
):
    """The READY move committed before the signal — a vanished delivery
    workflow (operator kill; S7 cancel) must not make the API deny an
    applied transition. 200 + the true status; the gap is ops' page."""
    order_id = place(client)
    advance_order(db_url, order_id, [*TO_ACCEPTED, ("ACCEPTED", "PREPARING")])
    saga.fail_with = SagaGone(f"dlv::{order_id}")
    r = client.post(f"/v1/restaurant/orders/{order_id}/ready", headers=OWNER)
    assert r.status_code == 200
    assert r.json() == {"order_id": order_id, "status": "READY"}


def test_ready_signal_failure_is_503_and_the_retry_heals(
    client, db_url, saga, place, advance_order
):
    """The crash-heal contract, failure half: the transition commits, the
    signal dies -> 503; the kitchen's retry replays the (now no-op) move
    and delivers the signal that was lost."""
    order_id = place(client)
    advance_order(db_url, order_id, [*TO_ACCEPTED, ("ACCEPTED", "PREPARING")])
    saga.fail_with = SagaUnavailable("temporal down")
    r = client.post(f"/v1/restaurant/orders/{order_id}/ready", headers=OWNER)
    assert r.status_code == 503
    assert saga.food_ready == []  # signal never landed...

    r = client.post(f"/v1/restaurant/orders/{order_id}/ready", headers=OWNER)
    assert r.status_code == 200  # ...but the state HAD moved; retry heals
    assert saga.food_ready == [order_id]


def test_decisions_during_the_unwind_classify_from_the_stamped_reason(
    client, db_url, saga, place, advance_order
):
    """begin_cancel stamps cancel_reason at CANCELLING, so the matrix
    answers correctly even while compensations are still retrying."""
    rejected = place(client)
    unwind = [*TO_CONFIRMED, ("CONFIRMED", "CANCELLING")]
    advance_order(db_url, rejected, unwind, reason="restaurant_rejected")
    r = client.post(f"/v1/restaurant/orders/{rejected}/reject", headers=OWNER)
    assert r.status_code == 200  # same verdict, mid-unwind: still a replay
    assert r.json()["status"] == "CANCELLING"
    r = client.post(f"/v1/restaurant/orders/{rejected}/accept", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"
    assert saga.decisions == []  # the fork is behind us; nothing signalled

    timed_out = place(client)
    advance_order(db_url, timed_out, unwind, reason="restaurant_timeout")
    r = client.post(f"/v1/restaurant/orders/{timed_out}/accept", headers=OWNER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_ALREADY_DECIDED"


def test_prep_routes_are_scoped(client, db_url, place, advance_order):
    order_id = place(client)
    advance_order(db_url, order_id, TO_ACCEPTED)
    for route in ("preparing", "ready"):
        assert (
            client.post(f"/v1/restaurant/orders/{order_id}/{route}", headers=OTHER).status_code
            == 404
        )
