"""The shared realtime plumbing: channel-authorizing tickets (GETDEL
single-use), bus lifecycle, and the stream generator's full state machine
— snapshot, relay, heartbeat, terminal close, jittered lifetime."""

import asyncio
import json

import pytest
from smartfood_realtime import RedisRealtime, StreamConfig, sse_event, stream_events


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

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):  # noqa: ASYNC109 — mirrors redis-py
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


# ── tickets ─────────────────────────────────────────────────────────


async def test_ticket_authorizes_a_channel_and_is_single_use():
    r = FakeRedis()
    bus = RedisRealtime(r)  # type: ignore[arg-type]
    await bus.put_ticket("tkt1", "sfo:notify:customer:usr_1", "usr_1", ttl_s=60)
    assert json.loads(r.store["sfo:ticket:tkt1"])["channel"] == "sfo:notify:customer:usr_1"
    first = await bus.consume_ticket("tkt1")
    assert first == {"channel": "sfo:notify:customer:usr_1", "sub": "usr_1"}
    assert await bus.consume_ticket("tkt1") is None  # GETDEL: gone means gone


async def test_publish_targets_the_named_channel():
    r = FakeRedis()
    await RedisRealtime(r).publish("sfo:track:ord_9", "CONFIRMED")  # type: ignore[arg-type]
    assert r.published == [("sfo:track:ord_9", "CONFIRMED")]


async def test_subscription_lifecycle_and_message_shapes():
    feed = [
        None,
        {"type": "subscribe", "data": 1},
        {"type": "message", "data": b"ACCEPTED"},
        {"type": "message", "data": "READY"},
    ]
    r = FakeRedis(feed)
    bus = RedisRealtime(r)  # type: ignore[arg-type]
    async with bus.subscription("sfo:track:ord_1") as sub:
        assert await sub.next_message() is None
        assert await sub.next_message() is None
        assert await sub.next_message() == "ACCEPTED"
        assert await sub.next_message() == "READY"
    assert r._pubsub.subscribed == ["sfo:track:ord_1"]
    assert r._pubsub.unsubscribed == ["sfo:track:ord_1"] and r._pubsub.closed


async def test_subscription_cleans_up_on_error():
    r = FakeRedis()
    with pytest.raises(RuntimeError):
        async with RedisRealtime(r).subscription("c"):  # type: ignore[arg-type]
            raise RuntimeError("stream blew up")
    assert r._pubsub.closed


async def test_aclose_closes_the_client():
    r = FakeRedis()
    await RedisRealtime(r).aclose()  # type: ignore[arg-type]
    assert r.closed


# ── the stream generator ────────────────────────────────────────────


class QueueBus:
    """A BusPort over an asyncio.Queue — blocks like the real adapter, so
    heartbeat timeouts are reachable."""

    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    def subscription(self, channel):
        queue = self.queue

        class _CM:
            async def __aenter__(self):
                class Sub:
                    async def next_message(self):
                        try:
                            return await asyncio.wait_for(queue.get(), timeout=1.0)
                        except TimeoutError:
                            return None

                return Sub()

            async def __aexit__(self, *exc):
                return False

        return _CM()


def cfg(**kw) -> StreamConfig:
    kw.setdefault("heartbeat_s", 0.5)
    kw.setdefault("lifetime_min_s", 30.0)
    kw.setdefault("lifetime_max_s", 30.0)
    return StreamConfig(**kw)


async def collect(gen, n):
    out = []
    async with asyncio.timeout(5.0):
        async for frame in gen:
            out.append(frame)
            if len(out) >= n:
                break
    return out


def test_sse_event_shape():
    assert sse_event("status", "CONFIRMED") == "event: status\ndata: CONFIRMED\n\n"


async def test_snapshot_then_relay_then_terminal_close():
    bus = QueueBus()
    bus.queue.put_nowait("ACCEPTED")
    bus.queue.put_nowait("SETTLED")
    frames = []
    async for frame in stream_events(
        "c",
        bus,
        cfg(),
        event_name="status",
        first="CONFIRMED",
        ends_stream=lambda s: s == "SETTLED",
    ):
        frames.append(frame)
    assert frames == [
        sse_event("status", "CONFIRMED"),
        sse_event("status", "ACCEPTED"),
        sse_event("status", "SETTLED"),
    ]  # generator RETURNED after the terminal frame


async def test_terminal_snapshot_is_a_single_frame():
    frames = [
        f
        async for f in stream_events(
            "c",
            QueueBus(),
            cfg(),
            event_name="status",
            first="CANCELLED",
            ends_stream=lambda s: s == "CANCELLED",
        )
    ]
    assert frames == [sse_event("status", "CANCELLED")]


async def test_no_snapshot_lane_heartbeats_and_relays():
    """The bell's shape: no `first`, nothing terminal — hints and
    heartbeats until the lifetime."""
    bus = QueueBus()
    gen = stream_events("c", bus, cfg(heartbeat_s=0.02), event_name="notify")
    frames = await collect(gen, 2)
    assert frames[0] == ": hb\n\n"  # quiet channel heartbeat came first
    bus.queue.put_nowait("customer")
    more = await collect(gen, 1)
    assert more == [sse_event("notify", "customer")]


async def test_lifetime_ends_with_reconnect():
    frames = [
        f
        async for f in stream_events(
            "c",
            QueueBus(),
            cfg(lifetime_min_s=0.03, lifetime_max_s=0.03, heartbeat_s=5.0),
            event_name="notify",
        )
    ]
    assert frames[-1] == sse_event("reconnect", "lifetime")


async def test_quiet_bus_tick_continues_silently():
    """next_message returning None (its own 1.0s poll tick) inside a longer
    heartbeat window: continue, no fake heartbeat, lifetime ends it."""
    frames = [
        f
        async for f in stream_events(
            "c",
            QueueBus(),
            cfg(lifetime_min_s=1.4, lifetime_max_s=1.4, heartbeat_s=5.0),
            event_name="notify",
        )
    ]
    assert frames.count(": hb\n\n") <= 1  # min(hb, remaining) boundary at most
    assert frames[-1] == sse_event("reconnect", "lifetime")


async def test_injected_rng_pins_the_jitter():
    calls = []

    def rng(lo, hi):
        calls.append((lo, hi))
        return 0.01

    frames = [
        f
        async for f in stream_events(
            "c",
            QueueBus(),
            cfg(lifetime_min_s=900.0, lifetime_max_s=1800.0, rng=rng),
            event_name="notify",
        )
    ]
    assert calls == [(900.0, 1800.0)]
    assert frames[-1] == sse_event("reconnect", "lifetime")


async def test_quiet_channels_heartbeat_on_polling_ticks():
    """The live find: a real bus returns None every ~1s poll tick, and each
    tick RESET the old timeout-based beat — 18 silent seconds on a live
    stream delivered zero bytes (test fakes BLOCKED, which live Redis never
    does, so coverage was green while every quiet stream would die behind a
    60s-idle ALB). The beat must accumulate across ticks."""
    import asyncio
    from collections.abc import AsyncGenerator
    from typing import cast

    from smartfood_realtime.stream import StreamConfig, stream_events

    class PollingBus:
        def subscription(self, channel):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                class Sub:
                    async def next_message(self):
                        await asyncio.sleep(0.01)  # a quiet poll tick
                        return None

                yield Sub()

            return cm()

    cfg = StreamConfig(heartbeat_s=0.05, rng=lambda a, b: 60.0)
    agen = cast(AsyncGenerator[str, None], stream_events("chan", PollingBus(), cfg, event_name="x"))
    frame = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
    assert frame == ": hb\n\n"  # silence accumulated into a beat
    await agen.aclose()
