"""The riders' socket: subprotocol auth, bound attribution, ingest
cadence, and the offer relay — all over injected fakes."""

import asyncio
import json
from contextlib import asynccontextmanager

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from rider_gateway.config import Settings
from rider_gateway.ingest import HB_TTL_S, LOC_TTL_S, LocationIngest, geo_key, hb_key, loc_key
from rider_gateway.main import WS_UNAUTHORIZED, create_app
from starlette.websockets import WebSocketDisconnect


class FakeVerifier:
    def __init__(self):
        self.tokens = {
            "good-rider": {"sub": "r1", "role": "rider", "rider_id": "r1"},
            "customer": {"sub": "usr_1", "role": "customer"},
        }

    async def verify(self, token: str) -> dict:
        claims = self.tokens.get(token)
        if claims is None:
            raise pyjwt.InvalidTokenError("nope")
        return claims


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis

    def geoadd(self, key, member):
        lon, lat, rider = member
        self._redis.geo.setdefault(key, {})[rider] = (lat, lon)

    def set(self, key, value, ex=None):
        self._redis.kv[key] = value
        self._redis.ttls[key] = ex

    async def execute(self):
        return []


class FakeRedis:
    def __init__(self):
        self.geo: dict = {}
        self.kv: dict = {}
        self.ttls: dict = {}

    def pipeline(self, transaction=False):
        return FakePipeline(self)


class RecordingProducer:
    def __init__(self):
        self.records: list[dict] = []

    async def send_nowait(self, topic, *, subject, schema, key, record):
        self.records.append(record)


class BoomProducer:
    async def send_nowait(self, topic, *, subject, schema, key, record):
        raise RuntimeError("kafka away")


class ScriptedRealtime:
    """One message per subscription, then quiet — enough to prove relay."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.subscribed: list[str] = []

    @asynccontextmanager
    async def subscription(self, channel):
        self.subscribed.append(channel)
        outer = self

        class Sub:
            async def next_message(self):
                if outer.messages:
                    return outer.messages.pop(0)
                await asyncio.sleep(0.02)  # quiet tick — never a busy loop
                return None

        yield Sub()


def _app(realtime=None, ingest=None):
    return create_app(
        Settings(),
        verifier=FakeVerifier(),  # type: ignore[arg-type]
        ingest=ingest,
        realtime=realtime,  # type: ignore[arg-type]
    )


def test_healthz():
    with TestClient(_app()) as client:  # `with` runs the lifespan
        assert client.get("/healthz").json()["service"] == "rider-gateway"


@pytest.mark.parametrize(
    "subprotocols",
    [None, ["bearer"], ["bearer", "bad-token"], ["bearer", "customer"], ["basic", "good-rider"]],
)
def test_handshake_refuses_everything_but_a_rider_jwt(subprotocols):
    client = TestClient(_app())
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/rider", subprotocols=subprotocols):
            pass  # pragma: no cover — the close arrives before any frame
    assert excinfo.value.code == WS_UNAUTHORIZED


def test_pings_write_the_index_with_bound_attribution():
    redis = FakeRedis()
    producer = RecordingProducer()
    ingest = LocationIngest(
        redis, cell="c1", producer=producer, topic="c1.rider.locations", sample_every=5
    )
    client = TestClient(_app(ingest=ingest))
    with client.websocket_connect("/ws/rider", subprotocols=["bearer", "good-rider"]) as ws:
        for i in range(6):
            ws.send_text(json.dumps({"type": "ping", "lat": 39.79 + i * 1e-4, "lon": -89.66}))
        ws.send_text(json.dumps({"type": "ping", "lat": 39.7999, "lon": -89.66}))
        ws.send_text("not json")  # dropped
        ws.send_text(json.dumps({"type": "mystery"}))  # dropped
        ws.send_text(json.dumps({"type": "ping", "lat": 999, "lon": 0}))  # out of range
        ws.send_text(json.dumps({"type": "ping", "lat": "x", "lon": 0}))  # wrong type
        ws.send_text(json.dumps({"type": "ping", "lat": 39.80, "lon": -89.66}))
    # Attribution came from the CONNECTION (r1), never the frame.
    assert "r1" in redis.geo[geo_key("c1")]
    assert redis.kv[loc_key("c1", "r1")].startswith("39.8")
    assert redis.ttls[loc_key("c1", "r1")] == LOC_TTL_S
    assert redis.ttls[hb_key("c1", "r1")] == HB_TTL_S
    # 8 VALID pings total → samples at #5 (every 5th); junk never counted.
    assert len(producer.records) == 1
    payload = json.loads(producer.records[0]["payload"])
    assert payload["rider_id"] == "r1"


def test_offers_relay_from_the_rider_channel():
    realtime = ScriptedRealtime([json.dumps({"type": "offer", "offer_id": "off_1"})])
    client = TestClient(_app(realtime=realtime))
    with client.websocket_connect("/ws/rider", subprotocols=["bearer", "good-rider"]) as ws:
        frame = json.loads(ws.receive_text())
    assert frame == {"type": "offer", "offer_id": "off_1"}
    assert realtime.subscribed == ["sfo:rider:r1"]


def test_socket_without_any_bus_still_accepts_pings():
    """Redis-less gateway (unit posture): the socket parks its relay and
    the receive loop still runs — REST remains the rider's floor."""
    client = TestClient(_app())
    with client.websocket_connect("/ws/rider", subprotocols=["bearer", "good-rider"]) as ws:
        ws.send_text(json.dumps({"type": "ping", "lat": 39.79, "lon": -89.66}))


async def test_ingest_swallows_producer_failure_and_noop_without_one():
    redis = FakeRedis()
    boom = LocationIngest(redis, cell="c1", producer=BoomProducer(), topic="t", sample_every=1)
    await boom.ping("r1", 39.79, -89.66, count=1)  # swallowed, logged
    quiet = LocationIngest(redis, cell="c1")  # no producer at all
    await quiet.ping("r1", 39.79, -89.66, count=1)
    assert redis.kv[loc_key("c1", "r1")] == "39.79,-89.66"
