"""`stream_relay` (ADR-0042).

One property is worth the whole file: a chunk published while the relay was
reading its snapshot must still reach the reader. That is the failure
`stream_events` has by construction and the reason this function exists, so
the test drives exactly that interleaving rather than trusting the ordering
by inspection.
"""

import json

from smartfood_realtime import Snapshot, StreamConfig, stream_relay
from smartfood_realtime.stream import BusPort


class Bus:
    """A bus whose messages can be queued DURING the snapshot read, which is
    the only way to exercise the window the ordering protects."""

    def __init__(self, messages: list[str] | None = None):
        self.messages = list(messages or [])
        self.subscribed = False
        self.events: list[str] = []

    def subscription(self, channel: str):
        bus = self

        class _Sub:
            async def next_message(self) -> str | None:
                return bus.messages.pop(0) if bus.messages else None

        class _CM:
            async def __aenter__(self):
                bus.subscribed = True
                bus.events.append("subscribe")
                return _Sub()

            async def __aexit__(self, *exc):
                return False

        return _CM()


def _chunk(seq: int, text: str, done: bool = False) -> str:
    return json.dumps({"seq": seq, "text": text, "done": done})


def _seq_of(raw: str) -> int:
    return int(json.loads(raw)["seq"])


def _is_done(raw: str) -> bool:
    return bool(json.loads(raw).get("done"))


CFG = StreamConfig(heartbeat_s=0.01, lifetime_min_s=0.3, lifetime_max_s=0.3, rng=lambda a, b: a)


async def _collect(bus: BusPort, snapshot, seq_upto: int = 0) -> list[str]:
    frames: list[str] = []
    async for frame in stream_relay(
        "sfo:assist:m1",
        bus,
        CFG,
        snapshot=snapshot,
        seq_upto=seq_upto,
        seq_of=_seq_of,
        ends_stream=_is_done,
    ):
        frames.append(frame)
    return frames


# ── the ordering, which is the whole point ──────────────────────────


async def test_a_chunk_published_during_the_snapshot_still_arrives():
    """THE test. The producer emits chunk 2 while we are reading the
    snapshot that only contains chunk 1. Subscribe-then-snapshot catches it;
    snapshot-then-subscribe loses it forever."""
    bus = Bus()

    async def snapshot() -> Snapshot:
        assert bus.subscribed, "the relay must subscribe before it snapshots"
        # The producer writes and publishes while we are in here.
        bus.messages.append(_chunk(2, "world", done=True))
        return Snapshot(chunks=[(1, _chunk(1, "hello"))], done=False)

    frames = await _collect(bus, snapshot)
    assert "hello" in frames[0] and "world" in frames[1]
    assert bus.events == ["subscribe"]


async def test_a_duplicate_across_the_window_is_dropped_not_shown():
    """The cost of the safe ordering: the same chunk can arrive twice, once
    from the table and once from the bus. The seq filter is what makes that
    invisible."""
    bus = Bus([_chunk(1, "hello"), _chunk(2, "world", done=True)])

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[(1, _chunk(1, "hello"))], done=False)

    frames = await _collect(bus, snapshot)
    assert len([f for f in frames if "hello" in f]) == 1


# ── resumption ──────────────────────────────────────────────────────


async def test_a_reconnect_replays_only_what_it_missed():
    bus = Bus([_chunk(4, "d", done=True)])

    async def snapshot() -> Snapshot:
        return Snapshot(
            chunks=[(1, _chunk(1, "a")), (2, _chunk(2, "b")), (3, _chunk(3, "c"))], done=False
        )

    frames = await _collect(bus, snapshot, seq_upto=2)
    bodies = "".join(frames)
    assert '"a"' not in bodies and '"b"' not in bodies  # already read
    assert '"c"' in bodies and '"d"' in bodies


async def test_every_frame_carries_its_seq_as_an_event_id():
    """`id:` is what lets a browser resume with Last-Event-ID and no client
    code at all (ADR-0042 §4)."""
    bus = Bus([_chunk(2, "b", done=True)])

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[(1, _chunk(1, "a"))], done=False)

    frames = await _collect(bus, snapshot)
    assert frames[0].startswith("id: 1\n")
    assert frames[1].startswith("id: 2\n")


async def test_a_message_that_finished_while_we_were_away_replays_and_closes():
    """Nothing will ever be published on that channel again; waiting for a
    terminal frame would hang until the lifetime expired."""
    bus = Bus()

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[(1, _chunk(1, "a")), (2, _chunk(2, "b"))], done=True)

    frames = await _collect(bus, snapshot)
    assert len(frames) == 2
    assert "reconnect" not in "".join(frames)


async def test_reconnecting_after_the_end_replays_nothing_and_closes():
    bus = Bus()

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[(1, _chunk(1, "a"))], done=True)

    assert await _collect(bus, snapshot, seq_upto=1) == []


# ── liveness ────────────────────────────────────────────────────────


async def test_a_quiet_channel_still_heartbeats():
    """Behind a proxy that reaps idle connections, a silent stream that
    sends no bytes is a stream that dies."""
    bus = Bus()

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[], done=False)

    frames = await _collect(bus, snapshot)
    assert any(f == ": hb\n\n" for f in frames)


async def test_the_lifetime_ends_a_stream_that_nobody_finished():
    """A producer that died mid-turn must not leave a connection held open
    forever."""
    bus = Bus()

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[], done=False)

    frames = await _collect(bus, snapshot)
    assert frames[-1] == "event: reconnect\ndata: lifetime\n\n"


async def test_the_terminal_chunk_closes_the_stream():
    bus = Bus([_chunk(1, "only", done=True)])

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[], done=False)

    frames = await _collect(bus, snapshot)
    assert len(frames) == 1 and "reconnect" not in frames[0]


async def test_a_blocking_subscription_still_heartbeats():
    """A fake bus returns None on every tick; a real one can BLOCK. The
    timeout is what keeps the beat owed on a bus that holds the call open —
    without it a quiet stream sends nothing and a proxy reaps it."""
    import asyncio

    class Blocking:
        """Standalone rather than a Bus subclass: it answers the same
        protocol with a different shape, which is a sibling, not an
        override."""

        def subscription(self, channel: str):
            class _Sub:
                async def next_message(self) -> str | None:
                    await asyncio.sleep(10)  # longer than any heartbeat
                    return None  # pragma: no cover — the timeout wins

            class _CM:
                async def __aenter__(self):
                    return _Sub()

                async def __aexit__(self, *exc):
                    return False

            return _CM()

    async def snapshot() -> Snapshot:
        return Snapshot(chunks=[], done=False)

    frames = await _collect(Blocking(), snapshot)
    assert any(f == ": hb\n\n" for f in frames)
    assert frames[-1] == "event: reconnect\ndata: lifetime\n\n"
