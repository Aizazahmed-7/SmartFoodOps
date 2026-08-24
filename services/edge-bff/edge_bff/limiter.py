"""Edge rate limiting — a fixed-window counter in Redis, failing OPEN.

Design, and the three decisions that matter:

1. **Fixed window, one atomic Redis call.** The counter key is
   `rl:{class}:{scope}:{bucket}` where bucket = epoch // window. One Lua
   script does INCR + first-hit EXPIRE atomically, so a crash can never
   leave an immortal key and a request costs exactly one round trip. The
   known artifact (a burst straddling a window boundary can pass ~2x the
   limit briefly) is acceptable at the edge — the limit protects capacity,
   it is not a billing meter. The upgrade path at real scale is a sliding
   window or token bucket behind this same interface; callers never know.

2. **Scope is identity when we have it, address when we don't.** Authed
   routes key by the verified `sub` — a NAT full of customers must not
   share one bucket, and one abusive user must not need an IP ban. Public
   routes (login, browse) key by client IP: it is all we have, and login
   brute force is exactly the attack the tight auth class exists for.

3. **Redis down = requests flow.** The same house contract as catalog's
   cache adapter: loss degrades, never corrupts — and never 5xxes. A rate
   limiter that hard-fails the whole edge when Redis blips converts a
   cache outage into a total outage; throttling is protection, and
   protection that detonates the thing it protects is worse than none.
   Failures are logged and counted (`rate_limit_errors_total`) so a
   fail-open storm is visible, not silent.

Limits are per route CLASS, not per path — three classes keep label
cardinality flat and map to intent: `auth` (credential guessing is the
attack), `read` (browse traffic, generous), `write` (money paths, between).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from prometheus_client import Counter
from redis import asyncio as aioredis
from redis.exceptions import RedisError
from smartfood_otel import REGISTRY, get_logger

log = get_logger("edge.limiter")

RATE_LIMITED = Counter(
    "rate_limited_total",
    "Requests refused with 429, by route class.",
    labelnames=("route_class",),
    registry=REGISTRY,
)
RATE_LIMIT_ERRORS = Counter(
    "rate_limit_errors_total",
    "Redis failures during a rate-limit check (each one failed OPEN).",
    registry=REGISTRY,
)

# INCR, and set the window TTL only on the key's first hit — refreshing the
# TTL on every request would silently turn the fixed window into a rolling
# one that never expires under sustained traffic.
_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return c
"""

_ERRORS = (RedisError, OSError, TimeoutError)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int  # when the current window rolls — the Retry-After anchor


class RateLimiter:
    """One instance per app. `client=None` = disarmed (unit tests, and any
    deployment without a REDIS_URL) — check() answers None and the proxy
    treats that as 'no verdict', exactly like tracing's empty endpoint."""

    def __init__(
        self,
        client: aioredis.Redis | None,
        *,
        limits: dict[str, int],
        window_seconds: int = 60,
        clock: Callable[[], float] = time.time,
    ):
        self._r = client
        self._limits = limits
        self._window = window_seconds
        self._clock = clock

    async def check(self, route_class: str, scope: str) -> Decision | None:
        limit = self._limits.get(route_class)
        if self._r is None or limit is None:
            return None
        bucket = int(self._clock()) // self._window
        key = f"rl:{route_class}:{scope}:{bucket}"
        try:
            count = int(await self._r.eval(_LUA, 1, key, str(self._window)))  # pyright: ignore[reportUnknownMemberType]
        except _ERRORS as exc:
            # Fail OPEN — and say so out loud, once per failure.
            RATE_LIMIT_ERRORS.inc()
            log.warning("rate-limit check failed — allowing", scope=scope, error=str(exc))
            return None
        if count > limit:
            RATE_LIMITED.labels(route_class=route_class).inc()
        return Decision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_epoch=(bucket + 1) * self._window,
        )

    async def aclose(self) -> None:
        if self._r is not None:
            await self._r.aclose()
