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
    deadline = asyncio.get_running_loop().time() + cfg.rng(cfg.lifetime_min_s, cfg.lifetime_max_s)
    async with bus.subscription(channel) as sub:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                yield sse_event("reconnect", "lifetime")
                return
            try:
                async with asyncio.timeout(min(cfg.heartbeat_s, remaining)):
                    message = await sub.next_message()
            except TimeoutError:
                yield ": hb\n\n"  # SSE comment — keeps proxies from reaping us
                continue
            if message is None:
                continue  # bus poll tick with nothing to say
            yield sse_event(event_name, message)
            if ends_stream(message):
                return
