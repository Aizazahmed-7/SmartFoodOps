"""Expiry reaper — the reservation ledger's garbage collector (FR-15).

A reservation whose saga died (worker crash, workflow stuck) must not hold
stock and a kitchen slot forever. The reaper releases overdue actives on an
interval; the guarded status transition makes it race-safe against a
late-arriving commit or release (whoever transitions first wins, the loser
sees a no-op).
"""

import asyncio

from smartfood_otel import get_logger

from .domain.service import InventoryService

log = get_logger("inventory.reaper")


class Reaper:
    def __init__(self, service: InventoryService, *, interval_seconds: float = 30.0):
        self._service = service
        self._interval = interval_seconds

    async def run(self) -> None:
        """Resilient loop: one bad pass logs and retries next interval.
        Cancellation is the shutdown signal (mirrors OutboxPoller.run)."""
        while True:
            try:
                await self._service.expire_overdue()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("reaper pass failed — will retry", error=str(exc))
            await asyncio.sleep(self._interval)
