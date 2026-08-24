"""RedisTracking against a duck-typed fake client: the GETDEL single-use
contract, channel naming, and subscription lifecycle (unsubscribe+close on
exit, even on error)."""

import json

import pytest
from order.adapters.tracking import RedisTracking, Subscription


class FakePubSub:
    def __init__(self, feed):
        self.feed = list(feed)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel):
        self.subscribed.append(channel)

    async def unsubscribe(self, channel):
        self.unsubscribed.append(channel)

    async def aclose(self):
        self.closed = True

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):  # noqa: ASYNC109 — mirrors redis-py's real signature
        return self.feed.pop(0) if self.feed else None


class FakeRedis:
    def __init__(self, feed=()):
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self._pubsub = FakePubSub(feed)
        self.closed = False

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def publish(self, channel, message):
        self.published.append((channel, message))

    def pubsub(self):
        return self._pubsub

    async def aclose(self):
        self.closed = True


async def test_ticket_roundtrip_is_single_use():
    r = FakeRedis()
    t = RedisTracking(r)  # type: ignore[arg-type]
    await t.put_ticket("tkt1", "ord_1", "usr_1", ttl_s=60)
    assert json.loads(r.store["sfo:ticket:tkt1"])["order_id"] == "ord_1"
    first = await t.consume_ticket("tkt1")
    assert first == {"order_id": "ord_1", "sub": "usr_1"}
    assert await t.consume_ticket("tkt1") is None  # GETDEL: gone means gone


async def test_publish_targets_the_order_channel():
    r = FakeRedis()
    await RedisTracking(r).publish("ord_9", "CONFIRMED")  # type: ignore[arg-type]
    assert r.published == [("sfo:track:ord_9", "CONFIRMED")]


async def test_subscription_lifecycle_and_message_shapes():
    feed = [
        None,  # quiet tick
        {"type": "subscribe", "data": 1},  # control noise — skip
        {"type": "message", "data": b"ACCEPTED"},  # bytes decode
        {"type": "message", "data": "READY"},  # str passthrough
    ]
    r = FakeRedis(feed)
    t = RedisTracking(r)  # type: ignore[arg-type]
    async with t.subscription("ord_1") as sub:
        assert await sub.next_status() is None
        assert await sub.next_status() is None
        assert await sub.next_status() == "ACCEPTED"
        assert await sub.next_status() == "READY"
    assert r._pubsub.subscribed == ["sfo:track:ord_1"]
    assert r._pubsub.unsubscribed == ["sfo:track:ord_1"]
    assert r._pubsub.closed


async def test_subscription_cleans_up_on_error():
    r = FakeRedis()
    t = RedisTracking(r)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        async with t.subscription("ord_1"):
            raise RuntimeError("stream blew up")
    assert r._pubsub.closed  # finally ran


async def test_aclose_closes_the_client():
    r = FakeRedis()
    await RedisTracking(r).aclose()  # type: ignore[arg-type]
    assert r.closed


def test_subscription_wrapper_is_constructible():
    assert Subscription(FakePubSub([]))._p is not None
