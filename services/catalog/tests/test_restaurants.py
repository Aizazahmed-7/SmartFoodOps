from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_owner", roles=frozenset({"customer"})))

BODY = {
    "name": "Biryani House",
    "city": "Springfield",
    "cuisines": ["BBQ ", "Pakistani", "bbq"],  # messy on purpose
}


def _admin(restaurant_id: str) -> dict:
    return headers_for(
        AuthContext(
            sub="usr_owner", roles=frozenset({"restaurant_admin"}), restaurant_id=restaurant_id
        )
    )


def _create(client) -> dict:
    return client.post("/v1/restaurants", json=BODY, headers=CUSTOMER).json()


def test_create_normalizes_and_dedupes(client):
    r = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert r.status_code == 201
    body = r.json()
    assert body["id"].startswith("brd_")  # onboarding returns the BRAND (ADR-0028)
    assert body["kind"] == "brand"
    assert [b["branch_label"] for b in body["branches"]] == ["Main"]
    assert body["branches"][0]["id"].startswith("rst_")
    assert body["cuisines"] == ["bbq", "pakistani"]  # slugged, deduped, order kept
    # Place-shaped fields belong to the location, not the template (0009):
    # the submitted city lands on the minted branch, and the brand has none.
    assert body["city"] is None and body["status"] is None
    assert body["branches"][0]["city"] == "springfield"
    assert body["branches"][0]["status"] == "open"


def test_create_requires_auth(client):
    assert client.post("/v1/restaurants", json=BODY).status_code == 401


def test_create_rejects_bad_input(client):
    no_cuisines = client.post("/v1/restaurants", json={**BODY, "cuisines": []}, headers=CUSTOMER)
    assert no_cuisines.status_code == 422
    bad_slug = client.post("/v1/restaurants", json={**BODY, "cuisines": ["b/bq"]}, headers=CUSTOMER)
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


def test_patch_by_owner_applies_and_announces(client):
    """The version this used to assert is off the wire (ADR-0037 onward), so
    the observable is the edit itself plus the event every mutation owes."""
    restaurant_id = _create(client)["id"]
    r = client.patch(
        f"/v1/restaurants/{restaurant_id}",
        json={"name": "Biryani Palace", "cuisines": ["pakistani"]},
        headers=_admin(restaurant_id),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Biryani Palace"
    assert r.json()["cuisines"] == ["pakistani"]  # replace-the-set
    assert "version" not in r.json()


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
    """Pause is a BRANCH action (0009): the brand is a menu template with
    no open/paused state. The claim still carries the BRAND id, so the
    owner header is unchanged — only the target moves."""
    body = _create(client)
    brand_id, branch_id = body["id"], body["branches"][0]["id"]
    paused = client.post(f"/v1/restaurants/{branch_id}/pause", headers=_admin(brand_id))
    assert paused.json()["status"] == "paused"
    assert client.get(f"/v1/restaurants/{branch_id}").json()["status"] == "paused"
    resumed = client.post(f"/v1/restaurants/{branch_id}/resume", headers=_admin(brand_id))
    assert resumed.json()["status"] == "open"


def test_pausing_a_brand_is_a_409(client):
    """Not a 404: the brand exists, it just has nothing to pause. Falling
    through to "unknown restaurant" would send an owner hunting for a
    missing row that is right there."""
    brand_id = _create(client)["id"]
    r = client.post(f"/v1/restaurants/{brand_id}/pause", headers=_admin(brand_id))
    assert r.status_code == 409
    assert "branches" in r.json()["error"]["message"]


def test_pause_wrong_restaurant_is_404(client):
    restaurant_id = _create(client)["id"]
    r = client.post(f"/v1/restaurants/{restaurant_id}/pause", headers=_admin("rst_other"))
    assert r.status_code == 404


def test_pause_vanished_restaurant_is_404(client):
    r = client.post("/v1/restaurants/rst_ghost/pause", headers=_admin("rst_ghost"))
    assert r.status_code == 404


def test_system_admin_bypasses_scoping(client):
    restaurant_id = _create(client)["id"]
    ops = headers_for(AuthContext(sub="usr_ops", roles=frozenset({"system_admin"})))
    r = client.patch(f"/v1/restaurants/{restaurant_id}", json={"name": "Renamed"}, headers=ops)
    assert r.status_code == 200
