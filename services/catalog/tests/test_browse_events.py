"""S8 browse telemetry: the fire-and-forget MenuViewed — identity capture,
anonymity, sampling, no-raise degrade, and the deterministic event id."""

import json

from catalog.adapters.browse import BrowseEvents
from catalog.config import Settings
from catalog.main import create_app
from fastapi.testclient import TestClient
from smartfood_auth import AuthContext, headers_for

from .conftest import FakeCache, FakeGrants

CUSTOMER = headers_for(AuthContext(sub="usr_9", role="customer"))


class RecordingProducer:
    def __init__(self, *, broken: bool = False):
        self.sent: list[dict] = []
        self.broken = broken

    async def send_nowait(self, topic, *, subject, schema, key, record, headers=None):
        if self.broken:
            raise RuntimeError("kafka is having a day")
        self.sent.append({"topic": topic, "key": key, "record": record})


def _browse(producer, rate=1.0, rng=lambda: 0.0):
    return BrowseEvents(
        producer,  # type: ignore[arg-type] — duck-typed on send_nowait
        topic="c1.browse.events",
        cell_id="c1",
        sample_rate=rate,
        rng=rng,
    )


def _app(browse):
    return create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True),
        grants=FakeGrants(),
        cache=FakeCache(),
        browse=browse,
    )


def _seed_restaurant(client):
    return client.post(
        "/v1/restaurants",
        json={"name": "Biryani House", "city": "springfield", "cuisines": ["pakistani"]},
        headers=CUSTOMER,
    ).json()["id"]


def test_menu_get_fires_a_view_with_the_stamped_identity():
    producer = RecordingProducer()
    app = _app(_browse(producer))
    with TestClient(app) as c:
        rid = _seed_restaurant(c)
        assert c.get(f"/v1/menus/{rid}", headers=CUSTOMER).status_code == 200
    (event,) = producer.sent
    assert event["topic"] == "c1.browse.events" and event["key"] == rid
    record = event["record"]
    assert record["event_type"] == "MenuViewed" and record["aggregate_type"] == "browse"
    payload = json.loads(record["payload"])
    assert payload["user_id"] == "usr_9" and payload["restaurant_id"] == rid


def test_anonymous_views_ride_with_null_identity():
    """public_read menus arrive unstamped — the view still counts (volume),
    with user_id null (excluded from conversion; you cannot join an order
    to a browser you cannot name)."""
    producer = RecordingProducer()
    app = _app(_browse(producer))
    with TestClient(app) as c:
        rid = _seed_restaurant(c)
        assert c.get(f"/v1/menus/{rid}").status_code == 200
    assert json.loads(producer.sent[-1]["record"]["payload"])["user_id"] is None


def test_two_requests_are_two_events_with_distinct_ids():
    """event_id is uuid5(request_id): deterministic per REQUEST (redelivery
    collapses on the consumer PK) while real repeat views stay distinct."""
    producer = RecordingProducer()
    app = _app(_browse(producer))
    with TestClient(app) as c:
        rid = _seed_restaurant(c)
        c.get(f"/v1/menus/{rid}")
        c.get(f"/v1/menus/{rid}")
    ids = [e["record"]["event_id"] for e in producer.sent]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_a_404_is_not_a_view():
    producer = RecordingProducer()
    app = _app(_browse(producer))
    with TestClient(app) as c:
        assert c.get("/v1/menus/rst_ghost").status_code == 404
    assert producer.sent == []


def test_sampling_gate_drops_below_the_rate():
    producer = RecordingProducer()
    app = _app(_browse(producer, rate=0.1, rng=lambda: 0.5))  # 0.5 >= 0.1 → dropped
    with TestClient(app) as c:
        rid = _seed_restaurant(c)
        c.get(f"/v1/menus/{rid}")
    assert producer.sent == []


def test_kafka_trouble_never_touches_the_menu_response():
    """The degrade contract: telemetry loss costs data points, never a
    customer's menu — and never a 5xx."""
    app = _app(_browse(RecordingProducer(broken=True)))
    with TestClient(app) as c:
        rid = _seed_restaurant(c)
        assert c.get(f"/v1/menus/{rid}").status_code == 200


def test_unconfigured_browse_is_a_clean_noop():
    app = _app(None)
    with TestClient(app) as c:
        rid = _seed_restaurant(c)
        assert c.get(f"/v1/menus/{rid}").status_code == 200
