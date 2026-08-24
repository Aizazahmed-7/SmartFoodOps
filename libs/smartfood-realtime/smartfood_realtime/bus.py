"""Tickets and the bus — one Redis client, both halves.

Tickets are the SSE auth design (FR-38): EventSource cannot set headers,
and a JWT in a query string soaks into access logs — so an authed POST
buys a 60-second single-use ticket and the stream redeems it with GETDEL:
atomic read-and-destroy, replay impossible by construction.

The bus is plain pub/sub, deliberately not a stream/queue: a live hint has
no value seconds later, so lost-on-disconnect is correct semantics and
there is nothing to reap. At scale this shards by channel hash across a
cluster; the interface never changes.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis


def _ticket_key(ticket: str) -> str:
    return f"sfo:ticket:{ticket}"


class RedisRealtime:
    def __init__(self, client: aioredis.Redis):
        self._r = client

    # ── tickets: authorize a CHANNEL ───────────────────────────────

    async def put_ticket(self, ticket: str, channel: str, sub: str, *, ttl_s: int) -> None:
        await self._r.set(
            _ticket_key(ticket), json.dumps({"channel": channel, "sub": sub}), ex=ttl_s
        )

    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None:
        """GETDEL: whoever redeems first owns it; everyone else gets None."""
        raw = await self._r.getdel(_ticket_key(ticket))  # pyright: ignore[reportUnknownMemberType]
        if raw is None:
            return None
        return json.loads(raw)

    # ── the bus ────────────────────────────────────────────────────

    async def publish(self, channel: str, data: str) -> None:
        await self._r.publish(channel, data)  # pyright: ignore[reportUnknownMemberType]

    @asynccontextmanager
    async def subscription(self, channel: str) -> AsyncIterator["Subscription"]:
        pubsub = self._r.pubsub()
        await pubsub.subscribe(channel)  # pyright: ignore[reportUnknownMemberType]
        try:
            yield Subscription(pubsub)
        finally:
            await pubsub.unsubscribe(channel)  # pyright: ignore[reportUnknownMemberType]
            await pubsub.aclose()

    async def aclose(self) -> None:
        await self._r.aclose()


class Subscription:
    def __init__(self, pubsub: Any):
        self._p = pubsub

    async def next_message(self) -> str | None:
        """One bus message, or None on this poll tick. The stream's
        heartbeat cadence does the waiting; this stays bounded so a silent
        channel still heartbeats on time."""
        message = await self._p.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None or message.get("type") != "message":
            return None
        data = message["data"]
        return data.decode() if isinstance(data, bytes) else str(data)
