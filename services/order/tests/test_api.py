# ── the internal courier relay (dispatch → dlv:: signals) ──────────


def _system_headers():
    from smartfood_auth import AuthContext, headers_for

    return headers_for(AuthContext(sub="svc:dispatch", role="system"))


def test_courier_events_relay_to_the_child(client, saga):
    for event, offer in (("accepted", "off_1"), ("picked_up", None), ("delivered", None)):
        r = client.post(
            "/v1/internal/orders/ord_1/courier",
            json={"event": event, "rider_id": "r_1", **({"offer_id": offer} if offer else {})},
            headers=_system_headers(),
        )
        assert r.status_code == 202, r.text
    assert saga.courier_events == [
        ("ord_1", "accepted", "r_1", "off_1"),
        ("ord_1", "picked_up", "r_1", None),
        ("ord_1", "delivered", "r_1", None),
    ]


def test_courier_accept_requires_the_offer_id(client):
    r = client.post(
        "/v1/internal/orders/ord_1/courier",
        json={"event": "accepted", "rider_id": "r_1"},
        headers=_system_headers(),
    )
    assert r.status_code == 422


def test_courier_events_map_saga_outcomes(client, saga):
    from order.domain.ports import SagaGone, SagaUnavailable

    saga.fail_with = SagaGone("dlv::ord_1")
    gone = client.post(
        "/v1/internal/orders/ord_1/courier",
        json={"event": "delivered", "rider_id": "r_1"},
        headers=_system_headers(),
    )
    assert gone.status_code == 404
    saga.fail_with = SagaUnavailable("temporal away")
    down = client.post(
        "/v1/internal/orders/ord_1/courier",
        json={"event": "delivered", "rider_id": "r_1"},
        headers=_system_headers(),
    )
    assert down.status_code == 503 and down.headers["Retry-After"] == "1"


def test_courier_events_are_system_only(client):
    from smartfood_auth import AuthContext, headers_for

    rider = headers_for(AuthContext(sub="r_1", role="rider", rider_id="r_1"))
    r = client.post(
        "/v1/internal/orders/ord_1/courier",
        json={"event": "delivered", "rider_id": "r_1"},
        headers=rider,
    )
    assert r.status_code == 403
