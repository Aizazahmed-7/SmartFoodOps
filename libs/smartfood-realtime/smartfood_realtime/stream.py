"""The SSE generator every lane shares: snapshot-first, hint relay,
heartbeats on quiet, and the jittered lifetime (FR-36) ending in an
explicit `reconnect` — so a fleet's reconnections spread, never thunder."""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class SubscriptionPort(Protocol):
    async def next_message(self) -> str | None: ...


class BusPort(Protocol):
    def subscription(self, channel: str) -> Any: ...  # async CM yielding SubscriptionPort


@dataclass(frozen=True)
class StreamConfig:
    ticket_ttl_s: int = 60
    heartbeat_s: float = 15.0
    lifetime_min_s: float = 900.0
    lifetime_max_s: float = 1800.0
    rng: Callable[[float, float], float] = field(default=random.uniform)


def sse_event(name: str, data: str) -> str:
    return f"event: {name}\ndata: {data}\n\n"


async def stream_events(
    channel: str,
    bus: BusPort,
    cfg: StreamConfig,
    *,
    event_name: str,
    first: str | None = None,
    ends_stream: Callable[[str], bool] = lambda _: False,
) -> AsyncIterator[str]:
    """Yield SSE frames for one connection.

    `first` is the snapshot (current truth, sent before any hint — no blank
    screens); `ends_stream` lets a lane close on terminal payloads (order
    tracking does; the bell never does — only the lifetime ends it)."""
    if first is not None:
        yield sse_event(event_name, first)
        if ends_stream(first):
            return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.rng(cfg.lifetime_min_s, cfg.lifetime_max_s)
    last_beat = loop.time()
    async with bus.subscription(channel) as sub:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                yield sse_event("reconnect", "lifetime")
                return
            try:
                async with asyncio.timeout(min(cfg.heartbeat_s, remaining)):
                    message = await sub.next_message()
            except TimeoutError:
                message = None  # a WEDGED bus still beats via the clock below
            if message is None:
                # The bus polls in ~1s ticks, so quiet channels arrive here
                # over and over — the beat is OWED once enough silent ticks
                # accumulate, not on a timeout that every tick resets. Found
                # live: 18 silent seconds, zero bytes on the wire — the old
                # timeout-only beat was unreachable off a polling bus (test
                # fakes BLOCKED, which live Redis never does), and behind a
                # 60s-idle ALB every quiet stream would have died at :60.
                if loop.time() - last_beat >= cfg.heartbeat_s:
                    yield ": hb\n\n"  # SSE comment — keeps proxies from reaping us
                    last_beat = loop.time()
                continue
            yield sse_event(event_name, message)
            last_beat = loop.time()
            if ends_stream(message):
                return


@dataclass(frozen=True)
class Snapshot:
    """What is already written for a message, and whether it is finished.

    `done` matters as much as the chunks: a reader reconnecting to a turn
    that ended while it was away must be replayed and closed, not left
    listening to a channel nobody will publish on again (ADR-0042 §5).
    """

    chunks: Sequence[tuple[int, str]]
    done: bool


def sse_chunk(name: str, data: str, *, seq: int) -> str:
    """An SSE frame carrying its sequence as the event id.

    `id:` is what makes resumption free: a browser stores the last one it
    saw and replays it as `Last-Event-ID` on reconnect, with no client code
    and no bespoke cursor parameter (ADR-0042 §4).
    """
    return f"id: {seq}\nevent: {name}\ndata: {data}\n\n"


async def stream_relay(
    channel: str,
    bus: BusPort,
    cfg: StreamConfig,
    *,
    snapshot: Callable[[], Awaitable[Snapshot]],
    seq_upto: int,
    event_name: str = "chunk",
    seq_of: Callable[[str], int] = lambda _: 0,
    ends_stream: Callable[[str], bool] = lambda _: False,
) -> AsyncIterator[str]:
    """Replay what a reader missed, then follow along — without a gap.

    **The ordering is the contract** (ADR-0042 §3), and it is the only reason
    this exists beside `stream_events`:

        subscribe  →  snapshot  →  relay, dropping seq <= what we replayed

    `stream_events` does the opposite — snapshot, then subscribe — which is
    correct for order tracking, where the next status hint repairs anything
    missed. For a token stream it is silent data loss: every chunk published
    in the gap is gone, and the reader sees an answer with a hole in it that
    nothing reports.

    Subscribing first makes that window produce DUPLICATES instead, and a
    duplicate is removable because every chunk carries a sequence number. A
    gap is not removable from anything the reader holds. The `seq` filter
    below is what pays for the safe ordering.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.rng(cfg.lifetime_min_s, cfg.lifetime_max_s)
    last_beat = loop.time()

    async with bus.subscription(channel) as sub:
        replayed = await snapshot()
        highest = seq_upto
        for seq, payload in replayed.chunks:
            if seq <= seq_upto:
                continue  # the reader already has it; this is a reconnect
            yield sse_chunk(event_name, payload, seq=seq)
            highest = max(highest, seq)
        if replayed.done:
            # Finished while we were away (or before we arrived). Nothing
            # more will ever be published here.
            return

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                yield sse_event("reconnect", "lifetime")
                return
            try:
                async with asyncio.timeout(min(cfg.heartbeat_s, remaining)):
                    message = await sub.next_message()
            except TimeoutError:
                message = None
            if message is None:
                if loop.time() - last_beat >= cfg.heartbeat_s:
                    yield ": hb\n\n"
                    last_beat = loop.time()
                continue
            seq = seq_of(message)
            # The terminal frame ends the stream WHATEVER its sequence. When
            # this sat behind the dedupe filter, a cursor above the live
            # sequence — a client storing one "last event id" globally
            # rather than per message, or a stale URL — swallowed the
            # terminal frame along with everything else, and the reader
            # heartbeated to the lifetime and reconnected forever. Resume is
            # per-MESSAGE (ADR-0042's consequences); a cursor from another
            # message must cost a replay, never a hang.
            if ends_stream(message):
                if seq > highest:
                    yield sse_chunk(event_name, message, seq=seq)
                return
            if seq <= highest:
                continue  # the duplicate the safe ordering bought us
            highest = seq
            yield sse_chunk(event_name, message, seq=seq)
            last_beat = loop.time()
