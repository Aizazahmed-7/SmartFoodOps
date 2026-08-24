"""Redis-backed tracking: single-use tickets and the status bus (FR-36/38).

Tickets are the SSE auth design: EventSource cannot set headers, and a JWT
in a query string would land in access logs and referrers — so the authed
POST buys a 60-second single-use ticket, and the stream endpoint redeems
it with GETDEL (atomic read-and-destroy: two connects with one ticket is
impossible by construction, not by bookkeeping).

The bus is plain Redis pub/sub, one channel per order. Deliberately NOT a
stream/queue: a tracking hint has no value five seconds later, so lost-on-
disconnect is the correct semantics and there is nothing to reap. At real
scale this shards by key hash across a cluster and the subscriber side
moves to the dedicated tracking-gateway fleet (ARCHITECTURE §5.1) — same
interface, different topology.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis


def _channel(order_id: str) -> str:
    return f"sfo:track:{order_id}"


def _ticket_key(ticket: str) -> str:
    return f"sfo:ticket:{ticket}"


class RedisTracking:
    """Both halves live on one client: the ticket store and the bus."""

    def __init__(self, client: aioredis.Redis):
        self._r = client

    # ── tickets ────────────────────────────────────────────────────

    async def put_ticket(self, ticket: str, order_id: str, sub: str, *, ttl_s: int) -> None:
        await self._r.set(
            _ticket_key(ticket), json.dumps({"order_id": order_id, "sub": sub}), ex=ttl_s
        )

    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None:
        """GETDEL: whoever redeems first owns it; everyone else gets None."""
        raw = await self._r.getdel(_ticket_key(ticket))  # pyright: ignore[reportUnknownMemberType]
        if raw is None:
            return None
        return json.loads(raw)

    # ── the bus ────────────────────────────────────────────────────

    async def publish(self, order_id: str, status: str) -> None:
        await self._r.publish(_channel(order_id), status)  # pyright: ignore[reportUnknownMemberType]

    @asynccontextmanager
    async def subscription(self, order_id: str) -> AsyncIterator["Subscription"]:
        pubsub = self._r.pubsub()
        await pubsub.subscribe(_channel(order_id))  # pyright: ignore[reportUnknownMemberType]
        try:
            yield Subscription(pubsub)
        finally:
            await pubsub.unsubscribe(_channel(order_id))  # pyright: ignore[reportUnknownMemberType]
            await pubsub.aclose()

    async def aclose(self) -> None:
        await self._r.aclose()


class Subscription:
    def __init__(self, pubsub: Any):
        self._p = pubsub

    async def next_status(self) -> str | None:
        """One bus message, or None on this poll tick. The stream's
        heartbeat cadence does the waiting; this stays non-blocking-ish so
        a silent channel still heartbeats on time."""
        message = await self._p.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is None or message.get("type") != "message":
            return None
        data = message["data"]
        return data.decode() if isinstance(data, bytes) else str(data)
