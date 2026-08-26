"""The HTTP surfaces over an injected service: role gates, ownership 404s,
conflict mapping, and the internal endpoints the workflow activities call."""

import pytest
from dispatch.config import Settings
from dispatch.domain.service import DispatchService
from dispatch.main import create_app
from fastapi.testclient import TestClient
from smartfood_auth import AuthContext, headers_for

from .test_service import FakeBus, FakeCourier, FakeGeo, RecordingEvents

SYSTEM = headers_for(AuthContext(sub="svc:order-worker", role="system"))
RIDER = headers_for(AuthContext(sub="r1", role="rider", rider_id="r1"))
CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))

PICKUP = {"lat": 39.7912, "lon": -89.6644}
DROPOFF = {"lat": 39.8025, "lon": -89.6478}


@pytest.fixture()
def client(riders, deliveries):
    service = DispatchService(
        riders=riders,
        deliveries=deliveries,
        geo=FakeGeo(),
        bus=FakeBus(),
        courier_events=FakeCourier(),
        events=RecordingEvents(),
        rider_cap=1,
        search_radius_km=3.0,
        widened_radius_km=6.0,
        widen_after_misses=3,
        offer_first_timeout_s=15.0,
        offer_next_timeout_s=12.0,
    )
    app = create_app(Settings(create_tables=False), service=service)
    with TestClient(app) as test_client:
        yield test_client


def _online(client, rider_headers=RIDER):
    return client.post("/v1/rider/status", json={"online": True, **PICKUP}, headers=rider_headers)


def _offer(client, order_id="ord_1"):
    return client.post(
        "/v1/internal/dispatch/offers",
        json={
            "order_id": order_id,
            "user_id": "usr_1",
            "restaurant_name": "Biryani House",
            "pickup": PICKUP,
            "dropoff": DROPOFF,
            "attempt": 1,
            "exclude": [],
        },
        headers=SYSTEM,
    )


def test_healthz(client):
    assert client.get("/healthz").json()["service"] == "dispatch"


def test_rider_surface_is_rider_only(client):
    assert client.get("/v1/rider/me", headers=CUSTOMER).status_code == 403
    assert client.get("/v1/rider/me").status_code == 401
    assert client.post("/v1/internal/dispatch/offers", json={}, headers=RIDER).status_code == 403


def test_going_online_requires_a_position(client):
    r = client.post("/v1/rider/status", json={"online": True}, headers=RIDER)
    assert r.status_code == 422
    assert _online(client).status_code == 200
    r = client.post("/v1/rider/status", json={"online": False}, headers=RIDER)
    assert r.json() == {"status": "offline"}


def test_the_full_tap_sequence(client):
    _online(client)
    offer = _offer(client).json()
    assert offer["outcome"] == "offered"
    me = client.get("/v1/rider/me", headers=RIDER).json()
    assert me["offer"]["order_id"] == "ord_1"  # the REST floor
    accepted = client.post(
        f"/v1/rider/offers/{offer['offer_id']}/accept",
        json={"order_id": "ord_1"},
        headers=RIDER,
    )
    assert accepted.json()["status"] == "assigned"
    assert client.post("/v1/rider/deliveries/ord_1/pickup", headers=RIDER).json() == {
        "status": "picked_up"
    }
    assert client.post("/v1/rider/deliveries/ord_1/deliver", headers=RIDER).json() == {
        "status": "delivered"
    }


def test_late_accept_maps_to_409(client):
    _online(client)
    offer = _offer(client).json()
    expire = client.post(
        "/v1/internal/dispatch/offers/expire",
        json={"order_id": "ord_1", "offer_id": offer["offer_id"], "rider_id": "r1"},
        headers=SYSTEM,
    )
    assert expire.json() == {"outcome": "revoked"}
    late = client.post(
        f"/v1/rider/offers/{offer['offer_id']}/accept",
        json={"order_id": "ord_1"},
        headers=RIDER,
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "ORDER_STATE_CONFLICT"


def test_foreign_taps_map_to_409(client):
    _online(client)
    offer = _offer(client).json()
    client.post(
        f"/v1/rider/offers/{offer['offer_id']}/accept", json={"order_id": "ord_1"}, headers=RIDER
    )
    intruder = headers_for(AuthContext(sub="r9", role="rider", rider_id="r9"))
    assert client.post("/v1/rider/deliveries/ord_1/pickup", headers=intruder).status_code == 409
    assert (
        client.post("/v1/rider/deliveries/ord_1/deliver", headers=RIDER).status_code == 409
    )  # not picked up yet


def test_internal_unassign_and_cancel(client):
    _online(client)
    offer = _offer(client).json()
    client.post(
        f"/v1/rider/offers/{offer['offer_id']}/accept", json={"order_id": "ord_1"}, headers=RIDER
    )
    unassigned = client.post(
        "/v1/internal/dispatch/orders/ord_1/unassign",
        json={"rider_id": "r1"},
        headers=SYSTEM,
    )
    assert unassigned.json() == {"outcome": "revoked"}
    cancelled = client.post("/v1/internal/dispatch/orders/ord_1/cancel", headers=SYSTEM)
    assert cancelled.json() == {"outcome": "cancelled"}


def test_courier_dot_is_ownership_scoped(client):
    _online(client)
    offer = _offer(client).json()
    client.post(
        f"/v1/rider/offers/{offer['offer_id']}/accept", json={"order_id": "ord_1"}, headers=RIDER
    )
    mine = client.get("/v1/deliveries/ord_1/courier", headers=CUSTOMER)
    assert mine.status_code == 200 and mine.json()["state"] == "ASSIGNED"
    intruder = headers_for(AuthContext(sub="usr_2", role="customer"))
    assert client.get("/v1/deliveries/ord_1/courier", headers=intruder).status_code == 404
    assert client.get("/v1/deliveries/ord_ghost/courier", headers=CUSTOMER).status_code == 404


def test_order_outage_maps_to_503_with_retry_after(riders, deliveries):
    class DownCourier(FakeCourier):
        async def send(self, order_id, *, event, rider_id):
            from dispatch.adapters.order_client import OrderUnavailable

            raise OrderUnavailable("down")

    service = DispatchService(
        riders=riders,
        deliveries=deliveries,
        geo=FakeGeo(),
        bus=FakeBus(),
        courier_events=DownCourier(),
        events=RecordingEvents(),
        rider_cap=1,
        search_radius_km=3.0,
        widened_radius_km=6.0,
        widen_after_misses=3,
        offer_first_timeout_s=15.0,
        offer_next_timeout_s=12.0,
    )
    with TestClient(create_app(Settings(create_tables=False), service=service)) as client:
        _online(client)
        offer = _offer(client).json()
        r = client.post(
            f"/v1/rider/offers/{offer['offer_id']}/accept",
            json={"order_id": "ord_1"},
            headers=RIDER,
        )
        assert r.status_code == 503 and r.headers["Retry-After"] == "1"
        # The DDB conversion HAPPENED before the notify failed — pickup and
        # deliver map the same outage the same way.
        assert client.post("/v1/rider/deliveries/ord_1/pickup", headers=RIDER).status_code == 503
        assert client.post("/v1/rider/deliveries/ord_1/deliver", headers=RIDER).status_code == 503
