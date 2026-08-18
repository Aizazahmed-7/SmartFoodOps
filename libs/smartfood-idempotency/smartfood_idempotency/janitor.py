"""The idempotency table's garbage collector.

Every other row in this system is reclaimed by something: outbox rows by
the partition dropper, reservations by the reaper, workflow history by
Temporal's retention. Idempotency keys had nobody — a COMPLETE row past
its 24h replay TTL, or an IN_PROGRESS row abandoned when a dependency was
down, simply lived forever. At demo scale that is invisible; at 2.5k
orders/s it is a table that only grows.

Shape deliberately mirrors the outbox poller: a resilient loop, one bad
pass logs and retries, cancellation is the shutdown signal. It SLEEPS
FIRST — booting a process should not trigger a table-wide delete, and unit
suites (which never reach the first tick) stay free of background writes.
"""

import asyncio

from smartfood_otel import get_logger

from .store import IdempotencyStore

log = get_logger("idempotency.janitor")


class IdempotencyJanitor:
    def __init__(
        self,
        store: IdempotencyStore,
        *,
        interval_seconds: float = 3600.0,
        orphan_ttl_seconds: float = 3600.0,
    ):
        self._store = store
        self._interval = interval_seconds
        self._orphan_ttl = orphan_ttl_seconds

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)  # sleep FIRST — see module docstring
            try:
                await self.purge_once()
            except Exception as exc:
                # Reclaiming storage is never urgent enough to crash a
                # process over; the next tick tries again. No CancelledError
                # clause: it derives from BaseException, so `except Exception`
                # already lets shutdown through — and with the sleep outside
                # this block, that is where cancellation lands anyway.
                log.warning("idempotency purge failed — will retry", error=str(exc))

    async def purge_once(self) -> int:
        purged = await self._store.purge(orphan_ttl_seconds=self._orphan_ttl)
        if purged:
            log.info("idempotency keys purged", rows=purged)
        return purged
