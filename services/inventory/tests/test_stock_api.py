"""Admin surface branches: stock upsert, capacity upsert, scoping, bounds."""

from smartfood_auth import AuthContext, headers_for


def admin(restaurant_id: str) -> dict[str, str]:
    return headers_for(
        AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=restaurant_id)
    )


SYSTEM = headers_for(AuthContext(sub="svc:order-worker", role="system"))


def test_set_stock_creates_then_updates(client):
    r = client.put(
        "/v1/inventory/restaurants/rst_1/stock/itm_a",
        json={"available": 40},
        headers=admin("rst_1"),
    )
    assert r.status_code == 200
    assert r.json() == {"item_id": "itm_a", "available": 40, "version": 0}

    r = client.put(
        "/v1/inventory/restaurants/rst_1/stock/itm_a",
        json={"available": 15},
        headers=admin("rst_1"),
    )
    assert r.json()["available"] == 15
    assert r.json()["version"] == 1  # update path bumps


def test_list_stock_scoped_and_ordered(client):
    for item in ("itm_b", "itm_a"):
        client.put(
            f"/v1/inventory/restaurants/rst_1/stock/{item}",
            json={"available": 5},
            headers=admin("rst_1"),
        )
    r = client.get("/v1/inventory/restaurants/rst_1/stock", headers=admin("rst_1"))
    assert [row["item_id"] for row in r.json()["items"]] == ["itm_a", "itm_b"]


def test_stock_response_carries_the_true_capacity(client):
    """The stock tab renders capacity from THIS response — found live when
    a typed draft haunted a sibling branch's screen and neither showed the
    real value. None = never provisioned."""
    r = client.get("/v1/inventory/restaurants/rst_1/stock", headers=admin("rst_1"))
    assert r.json()["capacity"] is None  # fresh location, no load row yet
    client.put(
        "/v1/inventory/restaurants/rst_1/capacity", json={"capacity": 5}, headers=admin("rst_1")
    )
    r = client.get("/v1/inventory/restaurants/rst_1/stock", headers=admin("rst_1"))
    assert r.json()["capacity"] == 5


def test_foreign_restaurant_is_404_not_403(client):
    r = client.get("/v1/inventory/restaurants/rst_other/stock", headers=admin("rst_1"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_same_item_id_keeps_independent_rows_per_branch(client):
    """The composite key (ADR-0028): a base item shared across branches has
    one fridge count PER branch — the second write creates a sibling row,
    never steals or 404s (StockScopeMismatch is unrepresentable now)."""
    client.put(
        "/v1/inventory/restaurants/rst_1/stock/itm_a",
        json={"available": 9},
        headers=admin("rst_1"),
    )
    r = client.put(
        "/v1/inventory/restaurants/rst_2/stock/itm_a",
        json={"available": 1},
        headers=SYSTEM,
    )
    assert r.status_code == 200
    assert r.json() == {"item_id": "itm_a", "available": 1, "version": 0}
    # and the original branch's row is untouched
    rows = client.get("/v1/inventory/restaurants/rst_1/stock", headers=admin("rst_1")).json()
    assert rows["items"][0]["available"] == 9


def test_stock_bounds_and_role_gate(client):
    assert (
        client.put(
            "/v1/inventory/restaurants/rst_1/stock/itm_a",
            json={"available": -1},
            headers=admin("rst_1"),
        ).status_code
        == 422
    )
    customer = {"X-Auth-Sub": "usr_c", "X-Auth-Role": "customer"}
    assert (
        client.put(
            "/v1/inventory/restaurants/rst_1/stock/itm_a",
            json={"available": 1},
            headers=customer,
        ).status_code
        == 403
    )


def test_capacity_upsert_and_bounds(client):
    r = client.put(
        "/v1/inventory/restaurants/rst_1/capacity", json={"capacity": 3}, headers=admin("rst_1")
    )
    assert r.json() == {"restaurant_id": "rst_1", "capacity": 3, "active": 0}
    r = client.put(
        "/v1/inventory/restaurants/rst_1/capacity", json={"capacity": 7}, headers=admin("rst_1")
    )
    assert r.json()["capacity"] == 7  # update path
    assert (
        client.put(
            "/v1/inventory/restaurants/rst_1/capacity",
            json={"capacity": 0},
            headers=admin("rst_1"),
        ).status_code
        == 422
    )
