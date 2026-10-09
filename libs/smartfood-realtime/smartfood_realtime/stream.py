"""The SSE generator every lane shares: snapshot-first, hint relay,
heartbeats on quiet, and the jittered lifetime (FR-36) ending in an
explicit `reconnect` — so a fleet's reconnections spread, never thunder."""

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class SubscriptionPort(Protocol):
    async def next_message(self) -> str | None: ...


class BusPort(Protocol):
    def subscription(self, channel: str) -> Any: ...  # async CM yielding SubscriptionPort


class ReadersPort(Protocol):
    async def readers(self, channel: str) -> int: ...


@dataclass(frozen=True)
class StreamConfig:
    ticket_ttl_s: int = 60
    heartbeat_s: float = 15.0
    lifetime_min_s: float = 900.0
    lifetime_max_s: float = 1800.0
    rng: Callable[[float, float], float] = field(default=random.uniform)


def sse_event(name: str, data: str) -> str:
    return f"event: {name}\ndata: {data}\n\n"


async def wait_for_reader(
    bus: ReadersPort, channel: str, *, timeout_s: float, poll_s: float = 0.02
) -> bool:
    """Block until somebody is listening on `channel`. True if they arrived.

    **Why a producer ever waits for a consumer.** The bus is pub/sub, so a
    frame published into a channel with no subscriber is dropped, not
    queued — which is correct for a live hint that has no value seconds
    later, and wrong for a token stream.

    The assistant's turn starts the instant the POST returns, and the reader
    cannot subscribe until it has read that response. Usually the turn spends
    the gap inside retrieval and a provider, so the reader wins the race by
    hundreds of milliseconds. But a safety refusal and an empty retrieval are
    answered WITHOUT a model: the whole answer is published within a
    millisecond of the task starting, into a channel nobody has reached yet,
    and the customer gets a blank bubble that hangs to the stream lifetime.

    Bounded, and the timeout is not an error: a client that never connects
    (closed the tab, crashed) must not hold a turn open. We wait, we give up,
    and the turn runs anyway — the durable answer is the `messages` row, and
    that is written either way.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if await bus.readers(channel):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_s, remaining))


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
