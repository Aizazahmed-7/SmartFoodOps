"""Slice 7 branches: the authoritative pricing read — auth, ownership,
missing ids, 86/paused reporting, bounds, cache bypass, torn reads."""

from smartfood_auth import AuthContext, headers_for

SYSTEM = headers_for(AuthContext(sub="svc:order-worker", role="system"))


def _seed(client):
    customer = headers_for(AuthContext(sub="usr_owner", role="customer"))
    rid = client.post(
        "/v1/restaurants",
        json={"name": "Biryani House", "city": "springfield", "cuisines": ["pakistani"]},
        headers=customer,
    ).json()["id"]
    admin = headers_for(
        AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=rid)
    )
    cat = client.post(
        f"/v1/restaurants/{rid}/categories", json={"name": "Mains"}, headers=admin
    ).json()
    biryani = client.post(
        f"/v1/restaurants/{rid}/items",
        json={
            "category_id": cat["id"], "name": "Biryani", "price_cents": 1200,
            "modifier_groups": [{
                "name": "Size", "min_select": 1, "max_select": 1,
                "options": [{"name": "Regular", "rank": 0},
                            {"name": "Family", "price_delta_cents": 600, "rank": 1}],
            }],
        },
        headers=admin,
    ).json()
    karahi = client.post(
        f"/v1/restaurants/{rid}/items",
        json={"category_id": cat["id"], "name": "Karahi", "price_cents": 1800},
        headers=admin,
    ).json()
    return rid, admin, biryani["id"], karahi["id"]


def _read(client, rid, item_ids, headers=SYSTEM):
    return client.get(
        f"/v1/internal/restaurants/{rid}/snapshot",
        params=[("item_ids", i) for i in item_ids],
        headers=headers,
    )


def test_pricing_read_shape(client, cache):
    rid, _, biryani, karahi = _seed(client)
    cache.ops.clear()
    r = _read(client, rid, [karahi, biryani, "itm_ghost"])
    assert r.status_code == 200
    body = r.json()
    assert body["restaurant"]["status"] == "open"
    assert body["restaurant"]["version"] == 4  # onboard + category + 2 items
    # Request order preserved, found only; ghost reported explicitly:
    assert [i["name"] for i in body["items"]] == ["Karahi", "Biryani"]
    assert body["missing_item_ids"] == ["itm_ghost"]
    family = body["items"][1]["modifier_groups"][0]["options"][1]
    assert (family["name"], family["price_delta_cents"]) == ("Family", 600)
    assert cache.ops == []  # money path NEVER touches the cache


def test_pricing_read_reports_86_and_paused(client):
    rid, admin, biryani, _ = _seed(client)
    client.patch(
        f"/v1/restaurants/{rid}/items/{biryani}", json={"available": False}, headers=admin
    )
    client.post(f"/v1/restaurants/{rid}/pause", headers=admin)
    body = _read(client, rid, [biryani]).json()
    # The read reports truth; the pricing library decides the rejection.
    assert body["items"][0]["available"] is False
    assert body["restaurant"]["status"] == "paused"


def test_pricing_read_cross_tenant_ids_are_missing(client):
    """Money-path ownership: another restaurant's item id must come back as
    missing, never priced — the WHERE clause is the guard."""
    rid_a, _, biryani, _ = _seed(client)
    customer_b = headers_for(AuthContext(sub="usr_b", role="customer"))
    rid_b = client.post(
        "/v1/restaurants",
        json={"name": "Burger Barn", "city": "springfield", "cuisines": ["burgers"]},
        headers=customer_b,
    ).json()["id"]
    body = _read(client, rid_b, [biryani]).json()
    assert body["items"] == []
    assert body["missing_item_ids"] == [biryani]


def test_pricing_read_auth_branches(client):
    rid, admin, biryani, _ = _seed(client)
    assert _read(client, rid, [biryani], headers={}).status_code == 401
    customer = headers_for(AuthContext(sub="usr_owner", role="customer"))
    assert _read(client, rid, [biryani], headers=customer).status_code == 403
    assert _read(client, rid, [biryani], headers=admin).status_code == 403  # system ONLY


def test_pricing_read_unknown_restaurant_is_404(client):
    assert _read(client, "rst_ghost", ["itm_x"]).status_code == 404


def test_pricing_read_bounds(client):
    rid, _, biryani, _ = _seed(client)
    no_ids = client.get(
        f"/v1/internal/restaurants/{rid}/snapshot", headers=SYSTEM
    )
    assert no_ids.status_code == 422  # item_ids required
    too_many = _read(client, rid, [f"itm_{n}" for n in range(51)])
    assert too_many.status_code == 422
    assert too_many.json()["error"]["details"][0]["field"] == "item_ids"
