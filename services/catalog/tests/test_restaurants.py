from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_owner", role="customer"))

BODY = {
    "name": "Biryani House",
    "city": "Springfield",
    "cuisines": ["BBQ ", "Pakistani", "bbq"],  # messy on purpose
}


def _admin(restaurant_id: str) -> dict:
    return headers_for(
        AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=restaurant_id)
    )


def _create(client) -> dict:
    return client.post("/v1/restaurants", json=BODY, headers=CUSTOMER).json()


def test_create_normalizes_and_dedupes(client):
    r = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert r.status_code == 201
    body = r.json()
    assert body["id"].startswith("rst_")
    assert body["cuisines"] == ["bbq", "pakistani"]  # slugged, deduped, order kept
    assert body["city"] == "springfield"
    assert body["status"] == "open"
    assert body["version"] == 1


def test_create_requires_auth(client):
    assert client.post("/v1/restaurants", json=BODY).status_code == 401


def test_create_rejects_bad_input(client):
    no_cuisines = client.post(
        "/v1/restaurants", json={**BODY, "cuisines": []}, headers=CUSTOMER
    )
    assert no_cuisines.status_code == 422
    bad_slug = client.post(
        "/v1/restaurants", json={**BODY, "cuisines": ["b/bq"]}, headers=CUSTOMER
    )
    assert bad_slug.status_code == 422
    unknown_field = client.post(
        "/v1/restaurants", json={**BODY, "status": "open"}, headers=CUSTOMER
    )
    assert unknown_field.status_code == 422


def test_get_is_public(client):
    restaurant_id = _create(client)["id"]
    r = client.get(f"/v1/restaurants/{restaurant_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "Biryani House"


def test_get_unknown_is_404(client):
    r = client.get("/v1/restaurants/rst_ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_patch_by_owner_bumps_version(client):
    restaurant_id = _create(client)["id"]
    r = client.patch(
        f"/v1/restaurants/{restaurant_id}",
        json={"name": "Biryani Palace", "cuisines": ["pakistani"]},
        headers=_admin(restaurant_id),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Biryani Palace"
    assert r.json()["cuisines"] == ["pakistani"]  # replace-the-set
    assert r.json()["version"] == 2


def test_patch_wrong_restaurant_is_404(client):
    restaurant_id = _create(client)["id"]
    r = client.patch(
        f"/v1/restaurants/{restaurant_id}", json={"name": "X"}, headers=_admin("rst_other")
    )
    assert r.status_code == 404  # not 403 — no existence leaks


def test_patch_customer_role_is_403(client):
    restaurant_id = _create(client)["id"]
    r = client.patch(f"/v1/restaurants/{restaurant_id}", json={"name": "X"}, headers=CUSTOMER)
    assert r.status_code == 403


def test_patch_empty_body_is_422(client):
    restaurant_id = _create(client)["id"]
    r = client.patch(f"/v1/restaurants/{restaurant_id}", json={}, headers=_admin(restaurant_id))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_patch_vanished_restaurant_is_404(client):
    r = client.patch("/v1/restaurants/rst_ghost", json={"name": "X"}, headers=_admin("rst_ghost"))
    assert r.status_code == 404


def test_pause_resume_cycle(client):
    restaurant_id = _create(client)["id"]
    paused = client.post(
        f"/v1/restaurants/{restaurant_id}/pause", headers=_admin(restaurant_id)
    )
    assert paused.json()["status"] == "paused"
    assert client.get(f"/v1/restaurants/{restaurant_id}").json()["status"] == "paused"
    resumed = client.post(
        f"/v1/restaurants/{restaurant_id}/resume", headers=_admin(restaurant_id)
    )
    assert resumed.json()["status"] == "open"
    assert resumed.json()["version"] == 3  # create, pause, resume


def test_pause_wrong_restaurant_is_404(client):
    restaurant_id = _create(client)["id"]
    r = client.post(f"/v1/restaurants/{restaurant_id}/pause", headers=_admin("rst_other"))
    assert r.status_code == 404


def test_pause_vanished_restaurant_is_404(client):
    r = client.post("/v1/restaurants/rst_ghost/pause", headers=_admin("rst_ghost"))
    assert r.status_code == 404


def test_system_admin_bypasses_scoping(client):
    restaurant_id = _create(client)["id"]
    ops = headers_for(AuthContext(sub="usr_ops", role="system_admin"))
    r = client.patch(f"/v1/restaurants/{restaurant_id}", json={"name": "Renamed"}, headers=ops)
    assert r.status_code == 200
