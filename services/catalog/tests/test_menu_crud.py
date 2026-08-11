"""Slice 4 branches: category CRUD, item CRUD with nested modifiers/tags,
the 86 toggle, cross-tenant 404s, and every validation rejection."""

from smartfood_auth import AuthContext, headers_for

ITEM = {
    "category_id": None,  # filled per test
    "name": "Chicken Biryani",
    "description": "House special",
    "price_cents": 1200,
    "tags": ["Spicy ", "halal"],
    "modifier_groups": [
        {
            "name": "Size",
            "min_select": 1,
            "max_select": 1,
            "rank": 0,
            "options": [
                {"name": "Regular", "price_delta_cents": 0, "rank": 0},
                {"name": "Family", "price_delta_cents": 600, "rank": 1},
            ],
        },
        {
            "name": "Add-ons",
            "min_select": 0,
            "max_select": 3,
            "rank": 1,
            "options": [{"name": "Raita", "price_delta_cents": 100}],
        },
    ],
}


def onboard(client, sub="usr_owner", name="Biryani House"):
    customer = headers_for(AuthContext(sub=sub, role="customer"))
    rid = client.post(
        "/v1/restaurants",
        json={"name": name, "city": "springfield", "cuisines": ["pakistani"]},
        headers=customer,
    ).json()["id"]
    admin = headers_for(AuthContext(sub=sub, role="restaurant_admin", restaurant_id=rid))
    return rid, admin


def add_category(client, rid, admin, name="Mains", rank=0):
    return client.post(
        f"/v1/restaurants/{rid}/categories", json={"name": name, "rank": rank}, headers=admin
    ).json()


def add_item(client, rid, admin, category_id, **overrides):
    body = {**ITEM, "category_id": category_id, **overrides}
    return client.post(f"/v1/restaurants/{rid}/items", json=body, headers=admin)


# ── categories ─────────────────────────────────────────────────────


def test_add_category(client):
    rid, admin = onboard(client)
    created = add_category(client, rid, admin)
    assert created["id"].startswith("cat_")
    assert created["name"] == "Mains"
    assert created["version"] == 2  # onboard=1, category=2


def test_add_category_auth_branches(client):
    rid, admin = onboard(client)
    customer = headers_for(AuthContext(sub="usr_owner", role="customer"))
    other = headers_for(
        AuthContext(sub="usr_x", role="restaurant_admin", restaurant_id="rst_other")
    )
    body = {"name": "Mains"}
    assert client.post(f"/v1/restaurants/{rid}/categories", json=body).status_code == 401
    assert (
        client.post(f"/v1/restaurants/{rid}/categories", json=body, headers=customer)
    ).status_code == 403
    assert (
        client.post(f"/v1/restaurants/{rid}/categories", json=body, headers=other)
    ).status_code == 404  # claim mismatch — no existence leak


def test_add_category_unknown_restaurant_is_404(client):
    ghost_admin = headers_for(
        AuthContext(sub="usr_g", role="restaurant_admin", restaurant_id="rst_ghost")
    )
    r = client.post(
        "/v1/restaurants/rst_ghost/categories", json={"name": "Mains"}, headers=ghost_admin
    )
    assert r.status_code == 404


def test_add_category_rejects_bad_input(client):
    rid, admin = onboard(client)
    assert (
        client.post(f"/v1/restaurants/{rid}/categories", json={"name": ""}, headers=admin)
    ).status_code == 422
    assert (
        client.post(f"/v1/restaurants/{rid}/categories", json={"name": "M", "x": 1}, headers=admin)
    ).status_code == 422


def test_update_category(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    r = client.patch(
        f"/v1/restaurants/{rid}/categories/{cat['id']}",
        json={"name": "Starters", "rank": 3},
        headers=admin,
    )
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["rank"]) == ("Starters", 3)
    assert r.json()["version"] == 3


def test_update_category_error_branches(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    empty = client.patch(f"/v1/restaurants/{rid}/categories/{cat['id']}", json={}, headers=admin)
    assert empty.status_code == 422
    unknown = client.patch(
        f"/v1/restaurants/{rid}/categories/cat_ghost", json={"name": "X"}, headers=admin
    )
    assert unknown.status_code == 404


def test_category_cross_tenant_is_404(client):
    rid1, admin1 = onboard(client, sub="usr_a", name="A")
    rid2, admin2 = onboard(client, sub="usr_b", name="B")
    cat2 = add_category(client, rid2, admin2)
    # admin1 addresses B's category through their own restaurant path:
    r = client.patch(
        f"/v1/restaurants/{rid1}/categories/{cat2['id']}", json={"name": "X"}, headers=admin1
    )
    assert r.status_code == 404  # ownership in the query


def test_delete_category(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    r = client.delete(f"/v1/restaurants/{rid}/categories/{cat['id']}", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "deleted"
    assert (
        client.delete(f"/v1/restaurants/{rid}/categories/{cat['id']}", headers=admin)
    ).status_code == 404  # already gone


def test_delete_category_with_items_is_409(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    assert add_item(client, rid, admin, cat["id"]).status_code == 201
    r = client.delete(f"/v1/restaurants/{rid}/categories/{cat['id']}", headers=admin)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CATEGORY_NOT_EMPTY"


# ── items ──────────────────────────────────────────────────────────


def test_add_item_full_shape(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    r = add_item(client, rid, admin, cat["id"])
    assert r.status_code == 201
    item = r.json()
    assert item["id"].startswith("itm_")
    assert item["tags"] == ["halal", "spicy"]  # slugged + sorted read order
    assert [g["name"] for g in item["modifier_groups"]] == ["Size", "Add-ons"]  # rank order
    assert [o["name"] for o in item["modifier_groups"][0]["options"]] == [
        "Regular",
        "Family",
    ]
    assert item["modifier_groups"][0]["options"][1]["price_delta_cents"] == 600
    assert item["available"] is True
    assert item["version"] == 3  # onboard, category, item


def test_add_item_category_branches(client):
    rid, admin = onboard(client, sub="usr_a", name="A")
    rid2, admin2 = onboard(client, sub="usr_b", name="B")
    cat2 = add_category(client, rid2, admin2)
    assert add_item(client, rid, admin, "cat_ghost").status_code == 404
    # B's category through A's path — ownership in the query:
    assert add_item(client, rid, admin, cat2["id"]).status_code == 404


def test_add_item_validation_branches(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    bad = [
        {"price_cents": -1},
        {"currency": "usd"},
        {"tags": ["not/ok"]},
        {
            "modifier_groups": [
                {
                    "name": "G",
                    "min_select": 2,
                    "max_select": 1,
                    "options": [{"name": "A"}, {"name": "B"}],
                }
            ]
        },
        {
            "modifier_groups": [
                {"name": "G", "min_select": 2, "max_select": 2, "options": [{"name": "A"}]}
            ]
        },
        {"modifier_groups": [{"name": "G", "options": []}]},
        {"unknown_field": 1},
    ]
    for overrides in bad:
        assert add_item(client, rid, admin, cat["id"], **overrides).status_code == 422


def test_item_86_toggle(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    item_id = add_item(client, rid, admin, cat["id"]).json()["id"]
    r = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"available": False}, headers=admin
    )
    assert r.status_code == 200
    assert r.json()["available"] is False
    menu = client.get(f"/v1/menus/{rid}").json()
    assert menu["categories"][0]["items"][0]["available"] is False


def test_item_move_category(client):
    rid, admin = onboard(client)
    cat1 = add_category(client, rid, admin, name="Mains", rank=0)
    cat2 = add_category(client, rid, admin, name="Specials", rank=1)
    item_id = add_item(client, rid, admin, cat1["id"]).json()["id"]
    moved = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}",
        json={"category_id": cat2["id"]},
        headers=admin,
    )
    assert moved.json()["category_id"] == cat2["id"]
    bad = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}",
        json={"category_id": "cat_ghost"},
        headers=admin,
    )
    assert bad.status_code == 404


def test_item_tags_replace_and_clear(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    item_id = add_item(client, rid, admin, cat["id"]).json()["id"]
    replaced = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"tags": ["Vegan"]}, headers=admin
    )
    assert replaced.json()["tags"] == ["vegan"]
    cleared = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"tags": []}, headers=admin
    )
    assert cleared.json()["tags"] == []


def test_item_modifiers_replace_and_clear(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    created = add_item(client, rid, admin, cat["id"]).json()
    old_group_ids = {g["id"] for g in created["modifier_groups"]}
    replaced = client.patch(
        f"/v1/restaurants/{rid}/items/{created['id']}",
        json={
            "modifier_groups": [
                {
                    "name": "Spice",
                    "min_select": 1,
                    "max_select": 1,
                    "options": [{"name": "Mild"}, {"name": "Hot"}],
                }
            ]
        },
        headers=admin,
    ).json()
    assert [g["name"] for g in replaced["modifier_groups"]] == ["Spice"]
    assert old_group_ids.isdisjoint({g["id"] for g in replaced["modifier_groups"]})
    cleared = client.patch(
        f"/v1/restaurants/{rid}/items/{created['id']}",
        json={"modifier_groups": []},
        headers=admin,
    ).json()
    assert cleared["modifier_groups"] == []


def test_item_update_error_branches(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    item_id = add_item(client, rid, admin, cat["id"]).json()["id"]
    empty = client.patch(f"/v1/restaurants/{rid}/items/{item_id}", json={}, headers=admin)
    assert empty.status_code == 422
    null_available = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"available": None}, headers=admin
    )
    assert null_available.status_code == 422  # explicit null ≠ omitted
    unknown = client.patch(
        f"/v1/restaurants/{rid}/items/itm_ghost", json={"name": "X"}, headers=admin
    )
    assert unknown.status_code == 404


def test_item_description_explicit_null_clears(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    item_id = add_item(client, rid, admin, cat["id"]).json()["id"]
    r = client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"description": None}, headers=admin
    )
    assert r.status_code == 200
    assert r.json()["description"] is None


def test_item_cross_tenant_is_404(client):
    rid1, admin1 = onboard(client, sub="usr_a", name="A")
    rid2, admin2 = onboard(client, sub="usr_b", name="B")
    cat2 = add_category(client, rid2, admin2)
    item2 = add_item(client, rid2, admin2, cat2["id"]).json()["id"]
    r = client.patch(f"/v1/restaurants/{rid1}/items/{item2}", json={"name": "X"}, headers=admin1)
    assert r.status_code == 404
    assert (
        client.delete(f"/v1/restaurants/{rid1}/items/{item2}", headers=admin1)
    ).status_code == 404


def test_delete_item(client):
    rid, admin = onboard(client)
    cat = add_category(client, rid, admin)
    item_id = add_item(client, rid, admin, cat["id"]).json()["id"]
    r = client.delete(f"/v1/restaurants/{rid}/items/{item_id}", headers=admin)
    assert r.status_code == 200
    assert client.get(f"/v1/menus/{rid}").json()["categories"][0]["items"] == []
    assert (
        client.delete(f"/v1/restaurants/{rid}/items/{item_id}", headers=admin)
    ).status_code == 404


# ── the public menu read ───────────────────────────────────────────


def test_menu_nested_and_ordered(client):
    rid, admin = onboard(client)
    mains = add_category(client, rid, admin, name="Mains", rank=1)
    add_category(client, rid, admin, name="Starters", rank=0)
    add_item(client, rid, admin, mains["id"], name="Karahi", rank=1, tags=[], modifier_groups=[])
    add_item(client, rid, admin, mains["id"], name="Biryani", rank=0, tags=[], modifier_groups=[])
    menu = client.get(f"/v1/menus/{rid}").json()
    assert menu["name"] == "Biryani House"
    assert menu["status"] == "open"
    assert menu["version"] == 5  # onboard + 2 categories + 2 items
    assert [c["name"] for c in menu["categories"]] == ["Starters", "Mains"]  # rank order
    assert [i["name"] for i in menu["categories"][1]["items"]] == ["Biryani", "Karahi"]


def test_menu_unknown_restaurant_is_404(client):
    r = client.get("/v1/menus/rst_ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
