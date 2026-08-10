"""Redis implementation of CachePort.

The port's no-raise contract lives HERE: every operation catches Redis/network
errors and degrades (miss / no-op / proceed), logging a warning. The domain
never learns Redis exists, let alone that it is down (docs §7: loss degrades,
never corrupts — and never 5xxes).
"""

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from smartfood_otel import get_logger

log = get_logger("catalog.cache")

_ERRORS = (RedisError, OSError)


class RedisCache:
    def __init__(self, client: aioredis.Redis):
        self._r = client

    async def get(self, key: str) -> str | None:
        try:
            value = await self._r.get(key)
        except _ERRORS:
            log.warning("cache get failed — treating as miss", key=key)
            return None
        # decode_responses=True gives str, but the client type can't promise
        # it — normalize so a misconfigured client can't leak bytes upward.
        return value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        try:
            await self._r.set(key, value, ex=ttl_seconds)
        except _ERRORS:
            log.warning("cache set failed — skipped", key=key)

    async def delete(self, key: str) -> None:
        try:
            await self._r.delete(key)
        except _ERRORS:
            log.warning("cache delete failed — key may serve stale until TTL", key=key)

    async def acquire_lock(self, key: str, ttl_ms: int) -> bool:
        try:
            return bool(await self._r.set(key, "1", nx=True, px=ttl_ms))
        except _ERRORS:
            return True  # no cache → no dedupe to win; render freely

    async def release_lock(self, key: str) -> None:
        try:
            await self._r.delete(key)
        except _ERRORS:
            pass  # the PX ttl releases it for us
