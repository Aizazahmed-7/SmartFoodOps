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


def test_restaurant_totals_aov_and_repeat_rate(client, app):
    """The lifetime block: revenue counts SETTLED only, AOV is integer-cents
    floor division, repeat rate = customers with >=2 orders / customers.
    Seed: usr_1 owns all four rst_1 orders (one settled at 2000) — one
    customer, repeat; add a second one-off customer for the denominator."""
    events = _seed_world()
    events.append(_event("OrderPlaced", "ord_e", at=_iso(5)))
    events[-1]["payload"]["user_id"] = "usr_2"  # this file's fixture uses dict payloads
    _fold(app, events)
    body = client.get("/v1/restaurant/analytics?days=7", headers=OWNER).json()
    t = body["totals"]
    assert t["orders"] == 5 and t["settled"] == 1 and t["cancelled"] == 2
    assert t["revenue_cents"] == 2000
    assert t["aov_cents"] == 2000  # 2000 // 1
    assert t["customers"] == 2 and t["repeat_customers"] == 1
    assert t["repeat_rate"] == 0.5
    assert body["window"] == {"orders": 5, "settled": 1, "cancelled": 2}


def test_restaurant_totals_with_no_sales_answer_none_not_zero(client, app):
    _fold(app, [_event("OrderPlaced", "ord_q")])
    t = client.get("/v1/restaurant/analytics?days=7", headers=OWNER).json()["totals"]
    assert t["settled"] == 0 and t["aov_cents"] is None and t["revenue_cents"] == 0


def test_ops_metrics_carry_windowed_revenue(client, app):
    _fold(app, _seed_world())
    body = client.get("/v1/internal/analytics/metrics?days=7", headers=SYSTEM).json()
    assert body["revenue_cents"] == 2000  # the one settled order


def _view_event(event_id, user, at):
    return {
        "event_type": "MenuViewed",
        "event_id": event_id,
        "payload": {"restaurant_id": "rst_1", "user_id": user, "viewed_at": at},
    }


def _fold_views(app, events):
    import asyncio

    from analytics.consumers import ViewsProjector

    asyncio.run(ViewsProjector(app.state.service._sessions).handle_batch(events))


def test_funnel_conversion_window_and_anonymity(client, app):
    """The three-way split the funnel must get right:
    usr_1 viewed then ordered 30m later      → converted
    usr_3 viewed then ordered 2 DAYS later   → viewer, not converted
    usr_1's second view AFTER the order      → the order cannot convert a
                                               view that hadn't happened
    two anonymous views                      → volume only
    """
    _fold(app, [_event("OrderPlaced", "ord_a", at=_iso(30))])  # usr_1, 30m ago
    _fold_views(
        app,
        [
            _view_event("v1", "usr_1", _iso(60)),  # 1h ago → order 30m later ✓
            _view_event("v2", "usr_3", _iso(60 * 49)),  # 49h ago → order 2 days after view ✗
            _view_event("v3", "usr_1", _iso(5)),  # AFTER the order ✗ (but usr_1 already ✓)
            _view_event("v4", None, _iso(10)),
            _view_event("v5", None, _iso(9)),
        ],
    )
    body = client.get("/v1/restaurant/analytics?days=7", headers=OWNER).json()
    f = body["funnel"]
    assert f["views"] == 5
    assert f["viewers"] == 2  # usr_1, usr_3 — anonymous excluded
    assert f["converted_viewers"] == 1  # usr_1 only
    assert f["conversion_rate"] == 0.5


def test_funnel_with_no_signed_in_viewers_answers_none(client, app):
    _fold_views(app, [_view_event("v8", None, _iso(5))])
    f = client.get("/v1/restaurant/analytics?days=7", headers=OWNER).json()["funnel"]
    assert f["views"] == 1 and f["viewers"] == 0 and f["conversion_rate"] is None
