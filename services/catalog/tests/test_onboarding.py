"""The self-serve onboarding contract: grant-after-commit, idempotent by
owner, repairable after a failed grant."""

from catalog.domain.ports import GrantRejected, GrantUnavailable
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_owner", role="customer"))

BODY = {"name": "Biryani House", "city": "Springfield", "cuisines": ["pakistani"]}


def test_create_triggers_the_grant(client, grants):
    r = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert r.status_code == 201
    assert grants.calls == [("usr_owner", r.json()["id"])]


def test_repeat_post_returns_existing_and_regrants(client, grants):
    """Replay as customer — the failed-grant repair scenario (never promoted)."""
    first = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    second = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]  # no second restaurant
    assert len(grants.calls) == 2  # grant re-attempted on the replay


def test_promoted_owner_can_replay_onboarding(client, grants):
    """After a SUCCESSFUL grant the owner is restaurant_admin — their replay
    must return the existing restaurant, not a 403 (found by the seed test)."""
    first = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    rid = first.json()["id"]
    promoted = headers_for(AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=rid))
    replay = client.post("/v1/restaurants", json=BODY, headers=promoted)
    assert replay.status_code == 200
    assert replay.json()["id"] == rid


def test_failed_grant_leaves_restaurant_and_retry_repairs(client, grants):
    grants.fail_with = GrantUnavailable("identity down")
    r = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["Retry-After"] == "1"

    # The restaurant committed BEFORE the grant ran (tx-boundary rule) —
    # the id is whatever the failed grant attempt was told about.
    restaurant_id = grants.calls[0][1]
    assert client.get(f"/v1/restaurants/{restaurant_id}").status_code == 200

    # Identity healed → the same POST becomes the repair path.
    grants.fail_with = None
    repaired = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert repaired.status_code == 200
    assert repaired.json()["id"] == restaurant_id
    assert len(grants.calls) == 2


def test_rejected_grant_is_409(client, grants):
    grants.fail_with = GrantRejected("role not grantable")
    r = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GRANT_CONFLICT"


def test_onboarding_role_gates(client, grants):
    rider = headers_for(AuthContext(sub="usr_r", role="rider", rider_id="rid_1"))
    assert client.post("/v1/restaurants", json=BODY, headers=rider).status_code == 403
    assert client.post("/v1/restaurants", json=BODY).status_code == 401
    # A foreign admin passes the ROLE gate (replays must — see above) but
    # Identity refuses the grant: one restaurant per user.
    foreign_admin = headers_for(
        AuthContext(sub="usr_other", role="restaurant_admin", restaurant_id="rst_other")
    )
    grants.fail_with = GrantRejected("already scoped to another restaurant")
    r = client.post("/v1/restaurants", json=BODY | {"name": "Second"}, headers=foreign_admin)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GRANT_CONFLICT"
