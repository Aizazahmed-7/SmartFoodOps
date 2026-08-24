"""Opening hours as an API concern: the timezone a restaurant carries, and
the open_now the pricing snapshot computes from it.

The hours ARITHMETIC is tested exhaustively in smartfood-pricing
(test_hours.py). What matters here is the wiring — that the column is
accepted, validated, persisted, echoed, and consulted at snapshot time."""

from unittest.mock import patch

from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_owner", role="customer"))
SYSTEM = headers_for(AuthContext(sub="svc:order", role="system"))

BODY = {"name": "Biryani House", "city": "springfield", "cuisines": ["pakistani"]}


def _admin(rid):
    return headers_for(AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=rid))


def test_new_restaurant_gets_the_configured_default_timezone(client):
    """An owner who never names a zone still gets a correct schedule for the
    deployment — the default is config, not a constant in a migration."""
    r = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER)
    assert r.status_code == 201
    assert r.json()["timezone"] == "America/Chicago"


def test_owner_can_name_their_own_timezone(client):
    r = client.post("/v1/restaurants", json={**BODY, "timezone": "Asia/Karachi"}, headers=CUSTOMER)
    assert r.status_code == 201 and r.json()["timezone"] == "Asia/Karachi"


def test_unknown_timezone_is_refused_at_the_boundary(client):
    """Validated on the way IN so is_open_at may treat an unknown zone as a
    data bug it degrades around, instead of a case it must reason about."""
    r = client.post(
        "/v1/restaurants", json={**BODY, "timezone": "Mars/Olympus_Mons"}, headers=CUSTOMER
    )
    assert r.status_code == 422
    assert "timezone" in str(r.json()["error"]["details"])


def test_timezone_is_updatable(client):
    rid = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER).json()["id"]
    r = client.patch(
        f"/v1/restaurants/{rid}", json={"timezone": "Europe/Berlin"}, headers=_admin(rid)
    )
    assert r.status_code == 200 and r.json()["timezone"] == "Europe/Berlin"


def test_update_rejects_an_unknown_timezone(client):
    rid = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER).json()["id"]
    r = client.patch(
        f"/v1/restaurants/{rid}", json={"timezone": "Nowhere/Land"}, headers=_admin(rid)
    )
    assert r.status_code == 422


def _snapshot(client, rid, item_ids=("itm_none",)):
    """item_ids is required by the read (bounded fan-out); the ids need not
    exist — an unknown id is reported in missing_item_ids, and the
    restaurant half of the snapshot is what these tests are about."""
    return client.get(
        f"/v1/internal/restaurants/{rid}/snapshot",
        params=[("item_ids", i) for i in item_ids],
        headers=SYSTEM,
    )


def test_snapshot_reports_open_now_true_without_hours(client):
    """No hours configured = never closed by schedule. This is what keeps
    every already-seeded restaurant sellable after this feature shipped."""
    rid = client.post("/v1/restaurants", json=BODY, headers=CUSTOMER).json()["id"]
    body = _snapshot(client, rid).json()
    assert body["restaurant"]["open_now"] is True


def test_snapshot_computes_open_now_from_hours_and_timezone(client):
    """The whole point: catalog owns the clock, so the snapshot carries a
    decided boolean and the pricing engine stays a pure function."""
    rid = client.post(
        "/v1/restaurants",
        json={**BODY, "timezone": "America/Chicago", "hours": {"mon": ["11:00", "23:00"]}},
        headers=CUSTOMER,
    ).json()["id"]

    # A Monday, 04:00 Chicago — outside the window.
    from datetime import UTC, datetime

    closed_at = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)  # 04:00 Chicago
    with patch("catalog.domain.service._now", return_value=closed_at):
        assert _snapshot(client, rid).json()["restaurant"]["open_now"] is False

    open_at = datetime(2026, 8, 24, 17, 0, tzinfo=UTC)  # 12:00 Chicago
    with patch("catalog.domain.service._now", return_value=open_at):
        assert _snapshot(client, rid).json()["restaurant"]["open_now"] is True


def test_status_and_schedule_are_independent(client):
    """Paused-but-in-hours is still shut. Two different questions, two
    different fields — a merged flag would lie to the customer."""
    rid = client.post(
        "/v1/restaurants",
        json={**BODY, "hours": {"mon": ["11:00", "23:00"]}},
        headers=CUSTOMER,
    ).json()["id"]
    client.post(f"/v1/restaurants/{rid}/pause", headers=_admin(rid))

    from datetime import UTC, datetime

    with patch("catalog.domain.service._now", return_value=datetime(2026, 8, 24, 17, tzinfo=UTC)):
        body = _snapshot(client, rid).json()["restaurant"]
    assert body["status"] == "paused" and body["open_now"] is True


async def test_timezone_rides_in_the_compacted_event_payload(grants, cache):
    """catalog.changes is COMPACTED, so any single surviving event must carry
    everything a consumer needs — the zone included, or a consumer rebuilding
    from one event would read the schedule against the wrong clock."""
    import sqlalchemy as sa
    from catalog.db import outbox

    from .test_domain import _service

    svc, sessions = await _service(grants, cache)
    restaurant, _ = await svc.create_restaurant(
        owner_user_id="usr_owner",
        name="Biryani House",
        city="springfield",
        cuisines=["pakistani"],
        lat=None,
        lon=None,
        hours={"mon": ["11:00", "23:00"]},
        timezone="Asia/Karachi",
    )
    async with sessions() as session:
        events = (await session.execute(sa.select(outbox))).all()

    assert events, "onboarding must stage an event"
    payload = events[-1].payload
    assert payload["timezone"] == "Asia/Karachi"
    assert payload["hours"] == {"mon": ["11:00", "23:00"]}
    assert restaurant.timezone == "Asia/Karachi"
