"""The thin adapters at their seams: geo over a scripted redis, the
courier client over MockTransport, the event emitter over a recorder."""

import httpx
import pytest
from dispatch.adapters.events import DispatchEvents
from dispatch.adapters.geo import HB_TTL_S, LOC_TTL_S, RiderGeo, geo_key, hb_key, loc_key
from dispatch.adapters.order_client import OrderCourierClient, OrderUnavailable


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._results: list[object] = []

    def geoadd(self, key, member):
        lon, lat, rider = member
        self._redis.geo.setdefault(key, {})[rider] = (lat, lon)
        self._results.append(1)

    def set(self, key, value, ex=None):
        self._redis.kv[key] = value
        self._redis.ttls[key] = ex
        self._results.append(True)

    def zrem(self, key, member):
        self._redis.geo.get(key, {}).pop(member, None)
        self._results.append(1)

    def delete(self, key):
        self._redis.kv.pop(key, None)
        self._results.append(1)

    def exists(self, key):
        self._results.append(1 if key in self._redis.kv else 0)

    async def execute(self):
        results, self._results = self._results, []
        return results


class FakeRedis:
    def __init__(self):
        self.geo: dict[str, dict[str, tuple[float, float]]] = {}
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    async def get(self, key):
        value = self.kv.get(key)
        return value.encode() if value is not None else None  # redis returns bytes

    async def geosearch(self, key, *, longitude, latitude, radius, unit, withdist, sort):
        from dispatch.domain.scoring import haversine_m

        rows = []
        for rider, (lat, lon) in self.geo.get(key, {}).items():
            distance_km = haversine_m(latitude, longitude, lat, lon) / 1000
            if distance_km <= radius:
                rows.append((rider.encode(), distance_km))
        return sorted(rows, key=lambda r: r[1])


@pytest.fixture()
def geo():
    redis = FakeRedis()
    return RiderGeo(redis, cell="c1"), redis


async def test_update_writes_index_loc_and_heartbeat_with_ttls(geo):
    rider_geo, redis = geo
    await rider_geo.update("r1", 39.79, -89.66)
    assert redis.geo[geo_key("c1")]["r1"] == (39.79, -89.66)
    assert redis.kv[loc_key("c1", "r1")] == "39.79,-89.66"
    assert redis.ttls[loc_key("c1", "r1")] == LOC_TTL_S
    assert redis.ttls[hb_key("c1", "r1")] == HB_TTL_S
    assert await rider_geo.latest("r1") == (39.79, -89.66)
    assert await rider_geo.latest("ghost") is None


async def test_remove_clears_every_trace(geo):
    rider_geo, redis = geo
    await rider_geo.update("r1", 39.79, -89.66)
    await rider_geo.remove("r1")
    assert "r1" not in redis.geo[geo_key("c1")]
    assert await rider_geo.latest("r1") is None


async def test_search_filters_dead_heartbeats_and_excluded(geo):
    rider_geo, redis = geo
    await rider_geo.update("r_alive", 39.792, -89.664)
    await rider_geo.update("r_dead", 39.793, -89.664)
    await rider_geo.update("r_skip", 39.794, -89.664)
    del redis.kv[hb_key("c1", "r_dead")]  # the 90s TTL lapsed — a ghost
    found = await rider_geo.search(39.7912, -89.6644, radius_km=3.0, exclude={"r_skip"})
    assert [rider for rider, _ in found] == ["r_alive"]
    assert found[0][1] == pytest.approx(96, rel=0.1)  # meters
    assert await rider_geo.search(0.0, 0.0, radius_km=3.0, exclude=set()) == []


# ── courier client ─────────────────────────────────────────────────


def _client(handler) -> OrderCourierClient:
    return OrderCourierClient(
        "http://order.test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_courier_ok_and_gone():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"status": "signalled"})

    assert await _client(handler).send("ord_1", event="accepted", rider_id="r1") == "ok"
    assert seen[0].url.path == "/v1/internal/orders/ord_1/courier"
    assert seen[0].headers["x-auth-role"] == "system"
    assert (
        await _client(lambda _: httpx.Response(404)).send("ord_1", event="delivered", rider_id="r1")
        == "gone"
    )


async def test_courier_5xx_retries_then_raises_and_4xx_is_loud():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with pytest.raises(OrderUnavailable):
        await _client(flaky).send("ord_1", event="accepted", rider_id="r1")
    assert calls["n"] == 3  # the retry budget was spent

    with pytest.raises(OrderUnavailable):
        await _client(lambda _: httpx.Response(422)).send("ord_1", event="accepted", rider_id="r1")


async def test_courier_network_trouble_retries():
    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(OrderUnavailable):
        await _client(refused).send("ord_1", event="accepted", rider_id="r1")


# ── event emitter ──────────────────────────────────────────────────


class RecordingProducer:
    def __init__(self, boom: bool = False):
        self.records: list[dict] = []
        self._boom = boom

    async def send_nowait(self, topic, *, subject, schema, key, record):
        if self._boom:
            raise RuntimeError("kafka away")
        self.records.append(record)


async def test_events_emit_deterministic_ids():
    producer = RecordingProducer()
    events = DispatchEvents(producer, topic="c1.dispatch.events", cell_id="c1")
    await events.rider_assigned("ord_1", rider_id="r1", offer_id="off_1")
    await events.rider_assigned("ord_1", rider_id="r1", offer_id="off_1")  # same fact
    await events.delivery_completed("ord_1", rider_id="r1")
    await events.rider_online("r1", session_marker="m1")
    await events.rider_offline("r1", session_marker="m1")
    ids = [r["event_id"] for r in producer.records]
    assert ids[0] == ids[1]  # deterministic — the consumer's PK absorbs the replay
    assert len(set(ids)) == 4
    assert producer.records[0]["aggregate_type"] == "delivery"
    assert producer.records[3]["aggregate_type"] == "rider"


async def test_events_disarmed_and_failing_are_quiet():
    disarmed = DispatchEvents(None, topic="t", cell_id="c1")
    await disarmed.rider_online("r1", session_marker="m")  # no producer — noop
    failing = DispatchEvents(RecordingProducer(boom=True), topic="t", cell_id="c1")
    await failing.delivery_completed("ord_1", rider_id="r1")  # swallowed, logged
