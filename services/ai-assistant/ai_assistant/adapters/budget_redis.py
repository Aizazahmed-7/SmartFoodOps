"""Redis-backed spend counters (ADR-0030 §5).

A fixed-window counter per key, named honestly rather than dressed up as a
token bucket: the failure mode is tolerating up to 2x budget across a
window boundary, which for a spend ceiling is noise, and the simplicity
means the hot path is one INCRBY.

Two deliberate choices:

* **Every key gets a TTL** via `EXPIRE ... NX` (NFR-13 — a key without a
  TTL is a CI-lintable defect here). `NX` rather than a "was this the
  first increment" test, because the latter loses the TTL entirely if the
  process dies between the two commands.
* **A refused request still counts.** The increment happens before the
  verdict, so a client hammering an exhausted budget stays locked out for
  the window instead of getting a free retry every time. For abuse that is
  the point; for an honest client it costs one window.
"""

from typing import Any


class RedisBudgetStore:
    def __init__(self, redis: Any) -> None:
        self._r = redis

    async def consume(self, key: str, tokens: int, *, budget: int, window_s: int) -> bool:
        total = int(await self._r.incrby(key, tokens))
        await self._r.expire(key, window_s, nx=True)
        return total <= budget
