"""The republish endpoint (B1's compaction repair).

`catalog.changes` is compacted, so the topic is supposed to BE the backup:
the last record per key carries the whole menu, which is why every payload
is full-state. B1 found that the topic had never actually been compacted —
it ran on the broker's delete default and its oldest records had aged out.
Fixing the policy cannot bring those back, so the catalog has to say its
truth again.
"""

from smartfood_auth import AuthContext, headers_for

SYSTEM = headers_for(AuthContext(sub="svc:ops", roles=frozenset({"system"})))
OWNER = headers_for(AuthContext(sub="usr_owner", roles=frozenset({"customer"})))


def _a_restaurant(client) -> str:
    created = client.post(
        "/v1/restaurants",
        json={
            "name": "Biryani House",
            "city": "springfield",
            "cuisines": ["pakistani"],
            "lat": 39.7,
            "lon": -89.6,
        },
        headers=OWNER,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_republish_stages_an_event_for_every_restaurant(client):
    """One full-state event per restaurant. On a compacted topic only the
    last per key survives — and that is the point: afterwards the log is a
    complete snapshot of the catalog again, so a downstream projection can
    be rebuilt from it with no bespoke backfill."""
    _a_restaurant(client)
    response = client.post("/v1/internal/catalog/republish", headers=SYSTEM)
    assert response.status_code == 200
    # A brand and its branch are both restaurants, so onboarding one
    # restaurant leaves at least one row to republish.
    assert response.json()["republished"] >= 1


def test_republish_is_idempotent(client):
    """Re-stating current truth is always safe: every payload is full
    state, so N republishes and one republish leave a compacted topic in
    exactly the same condition."""
    _a_restaurant(client)
    first = client.post("/v1/internal/catalog/republish", headers=SYSTEM).json()
    again = client.post("/v1/internal/catalog/republish", headers=SYSTEM).json()
    assert first == again


def test_republish_is_system_only(client):
    """It writes a row per restaurant. An owner must not be able to fire
    that burst, and the gateway never routes `/v1/internal/*` anyway."""
    assert client.post("/v1/internal/catalog/republish").status_code == 401
    assert client.post("/v1/internal/catalog/republish", headers=OWNER).status_code == 403
