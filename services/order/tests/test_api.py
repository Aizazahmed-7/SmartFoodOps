# ── the internal courier relay (dispatch → dlv:: signals) ──────────


def _system_headers():
    from smartfood_auth import AuthContext, headers_for

    return headers_for(AuthContext(sub="svc:dispatch", roles=frozenset({"system"})))


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

    rider = headers_for(AuthContext(sub="r_1", roles=frozenset({"rider"}), rider_id="r_1"))
    r = client.post(
        "/v1/internal/orders/ord_1/courier",
        json={"event": "delivered", "rider_id": "r_1"},
        headers=rider,
    )
    assert r.status_code == 403


# ── the internal recipients read (notification → refund workflow) ──


def test_recipients_returns_both_parties_for_a_real_order(
    client, catalog, make_snapshot, place_order
):
    """Notification asks this because payment events are keyed by order and
    carry no user_id (ADR-0040). Unscoped by design — the caller is a
    service acting on an event, not a user reading their own order."""
    catalog.snapshot = make_snapshot()
    order_id = place_order(client)
    r = client.get(f"/v1/internal/orders/{order_id}/recipients", headers=_system_headers())
    assert r.status_code == 200
    assert r.json() == {"user_id": "usr_1", "restaurant_id": "rst_1"}


def test_recipients_for_an_unknown_order_is_404(client):
    r = client.get("/v1/internal/orders/ord_ghost/recipients", headers=_system_headers())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_recipients_requires_a_system_claim(client, catalog, make_snapshot, place_order):
    """It leaks the customer id for any order id, so it must never be
    reachable with a customer's own token."""
    from smartfood_auth import AuthContext, headers_for

    catalog.snapshot = make_snapshot()
    order_id = place_order(client)
    customer = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
    assert (
        client.get(f"/v1/internal/orders/{order_id}/recipients", headers=customer).status_code
        == 403
    )
