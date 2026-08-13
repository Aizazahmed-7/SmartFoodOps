"""GET /v1/orders/{id} + the keyset-paginated history: ownership, cursor
walking, bounds."""

from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))
OTHER = headers_for(AuthContext(sub="usr_2", role="customer"))


def test_detail_returns_snapshots_not_live_menu(client, catalog, make_snapshot, place_order):
    catalog.snapshot = make_snapshot()
    order_id = place_order(client)
    detail = client.get(f"/v1/orders/{order_id}", headers=CUSTOMER).json()
    assert detail["status"] == "PLACED"
    assert detail["restaurant_name"] == "Biryani House"
    assert detail["items"][0]["name"] == "Chicken Biryani"
    assert detail["totals"]["total_cents"] > 0
    assert detail["delivery_address"]["line1"] == "12 Mango St"
    assert detail["currency"] == "USD"


def test_detail_not_yours_and_unknown_are_the_same_404(client, catalog, make_snapshot, place_order):
    catalog.snapshot = make_snapshot()
    order_id = place_order(client)
    not_yours = client.get(f"/v1/orders/{order_id}", headers=OTHER)
    unknown = client.get("/v1/orders/ord_ghost", headers=CUSTOMER)
    assert not_yours.status_code == unknown.status_code == 404
    assert not_yours.json()["error"]["code"] == unknown.json()["error"]["code"] == "NOT_FOUND"


def test_history_walks_pages_newest_first(client, catalog, identity, make_snapshot, place_order):
    catalog.snapshot = make_snapshot()
    placed = [place_order(client) for _ in range(5)]

    first = client.get("/v1/orders?limit=2", headers=CUSTOMER).json()
    assert [o["order_id"] for o in first["items"]] == list(reversed(placed))[:2]
    assert first["next_cursor"] is not None

    def page_after(cursor):
        return client.get(f"/v1/orders?limit=2&cursor={cursor}", headers=CUSTOMER).json()

    second = page_after(first["next_cursor"])
    assert len(second["items"]) == 2
    third = page_after(second["next_cursor"])
    assert len(third["items"]) == 1
    assert third["next_cursor"] is None  # the end

    walked = [o["order_id"] for page in (first, second, third) for o in page["items"]]
    assert walked == list(reversed(placed))  # every order exactly once, newest first
    assert all(o["total_cents"] > 0 and o["restaurant_name"] for o in first["items"])


def test_history_is_user_scoped(client, catalog, identity, make_snapshot, place_order):
    catalog.snapshot = make_snapshot()
    identity.addresses[("usr_2", "adr_1")] = identity.addresses[("usr_1", "adr_1")]
    place_order(client)
    place_order(client, headers=OTHER)
    mine = client.get("/v1/orders", headers=CUSTOMER).json()
    theirs = client.get("/v1/orders", headers=OTHER).json()
    assert len(mine["items"]) == 1
    assert len(theirs["items"]) == 1
    assert mine["items"][0]["order_id"] != theirs["items"][0]["order_id"]


def test_malformed_cursor_is_422(client, catalog, make_snapshot):
    catalog.snapshot = make_snapshot()
    r = client.get("/v1/orders?cursor=not-base64!!", headers=CUSTOMER)
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [{"field": "cursor", "issue": "not a valid cursor"}]


def test_limit_bounds(client, catalog):
    assert client.get("/v1/orders?limit=0", headers=CUSTOMER).status_code == 422
    assert client.get("/v1/orders?limit=101", headers=CUSTOMER).status_code == 422
    assert client.get("/v1/orders?limit=100", headers=CUSTOMER).status_code == 200
