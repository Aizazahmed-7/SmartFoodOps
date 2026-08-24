"""The two read surfaces: metric math from scripted facts, role gates, and
the claim-scoped tenancy (cross-tenant reads are unrepresentable)."""

from datetime import UTC, datetime, timedelta

from analytics.consumers import FactsProjector
from smartfood_auth import AuthContext, headers_for

SYSTEM = headers_for(AuthContext(sub="svc:ops", role="system"))
OWNER = headers_for(AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id="rst_1"))
OTHER_OWNER = headers_for(
    AuthContext(sub="usr_other", role="restaurant_admin", restaurant_id="rst_2")
)
CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))


def _iso(minutes_ago=0):
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _event(event_type, order_id, restaurant="rst_1", at=None, reason=None, cents=1000):
    payload = {
        "order_id": order_id,
        "user_id": "usr_1",
        "restaurant_id": restaurant,
        "status": {
            "OrderPlaced": "PLACED",
            "OrderConfirmed": "CONFIRMED",
            "OrderDelivered": "DELIVERED",
            "OrderCancelled": "CANCELLED",
            "OrderSettled": "SETTLED",
        }[event_type],
        "aggregate_version": 1,
        "totals": {"totals": {"total_cents": cents}},
        "occurred_at": at or _iso(),
    }
    if reason:
        payload["cancel_reason"] = reason
    return {"event_type": event_type, "payload": payload}


def _seed_world():
    """Four orders at rst_1: settled, delivered-not-settled, rejected,
    customer-cancelled — plus one foreign order at rst_2."""
    return [
        # ord_a: the full happy path (revenue!)
        _event("OrderPlaced", "ord_a", at=_iso(50)),
        _event("OrderConfirmed", "ord_a", at=_iso(49)),
        _event("OrderDelivered", "ord_a", at=_iso(20)),
        _event("OrderSettled", "ord_a", at=_iso(19), cents=2000),
        # ord_b: delivered but not yet settled (no revenue yet)
        _event("OrderPlaced", "ord_b", at=_iso(40)),
        _event("OrderConfirmed", "ord_b", at=_iso(39)),
        _event("OrderDelivered", "ord_b", at=_iso(10)),
        # ord_c: the restaurant rejected it
        _event("OrderPlaced", "ord_c", at=_iso(30)),
        _event("OrderConfirmed", "ord_c", at=_iso(29)),
        _event("OrderCancelled", "ord_c", at=_iso(28), reason="restaurant_rejected"),
        # ord_d: customer cancelled — counts against cancellation, NOT acceptance
        _event("OrderPlaced", "ord_d", at=_iso(25)),
        _event("OrderConfirmed", "ord_d", at=_iso(24)),
        _event("OrderCancelled", "ord_d", at=_iso(23), reason="customer_cancelled"),
        # a different restaurant's order — must never leak into rst_1's view
        _event("OrderPlaced", "ord_x", restaurant="rst_2", at=_iso(15)),
    ]


def _fold(app, events):
    """The projector is loop-agnostic; fold on the test's own loop (the
    StaticPool sqlite engine is shared with the app's portal thread)."""
    import asyncio

    asyncio.run(FactsProjector(app.state.service._sessions).handle_batch(events))


def test_ops_metrics_math(client, app):
    _fold(app, _seed_world())
    body = client.get("/v1/internal/analytics/metrics?days=7", headers=SYSTEM).json()
    assert body["total_orders"] == 5
    assert {"restaurant_id": "rst_1", "orders": 4} in body["orders_per_restaurant"]
    assert body["peak_hour"] is not None and body["peak_hour"]["orders"] >= 1
    # ord_a delivered 30m after placing; ord_b 30m too — avg ≈ 1800s
    assert 1700 < body["avg_delivery_seconds"] < 1900
    assert body["cancellation_rate"] == 0.4  # 2 of 5
    assert body["acceptance_rate"] == 0.75  # 1 rejection / 4 confirmed
    assert body["delivery_success_rate"] == 0.5  # 2 delivered / 4 confirmed
    assert body["rider_utilization"] is None  # blocked on dispatch — honest null


def test_ops_metrics_on_an_empty_window_answers_nulls_not_zeros(client):
    body = client.get("/v1/internal/analytics/metrics?days=1", headers=SYSTEM).json()
    assert body["total_orders"] == 0
    assert body["cancellation_rate"] is None  # no data ≠ perfectly zero
    assert body["peak_hour"] is None and body["avg_delivery_seconds"] is None


def test_ops_metrics_is_system_only(client):
    assert client.get("/v1/internal/analytics/metrics", headers=CUSTOMER).status_code == 403
    assert client.get("/v1/internal/analytics/metrics", headers=OWNER).status_code == 403


def test_restaurant_view_is_scoped_by_the_claim(client, app):
    _fold(app, _seed_world())
    mine = client.get("/v1/restaurant/analytics?days=7", headers=OWNER).json()
    assert mine["restaurant_id"] == "rst_1"
    assert sum(d["orders"] for d in mine["days"]) == 4  # ord_x invisible
    assert sum(d["revenue_cents"] for d in mine["days"]) == 2000  # settled only
    theirs = client.get("/v1/restaurant/analytics?days=7", headers=OTHER_OWNER).json()
    assert sum(d["orders"] for d in theirs["days"]) == 1


def test_restaurant_view_requires_the_role_and_the_claim(client):
    assert client.get("/v1/restaurant/analytics", headers=CUSTOMER).status_code == 403
    no_claim = headers_for(AuthContext(sub="usr_o", role="restaurant_admin"))
    assert client.get("/v1/restaurant/analytics", headers=no_claim).status_code == 404


def test_days_bounds(client):
    assert client.get("/v1/internal/analytics/metrics?days=0", headers=SYSTEM).status_code == 422
    assert client.get("/v1/internal/analytics/metrics?days=91", headers=SYSTEM).status_code == 422
