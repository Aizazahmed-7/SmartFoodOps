"""The placement sweeper — closes the commit→start gap (FR-14's last hole).

Placement commits the order, THEN starts the saga — never network inside
an open transaction. A crash in that gap leaves a PLACED row whose
workflow never existed: stock unreserved, card unauthorized, customer
watching a spinner that will never move. The committed row IS the durable
to-do; this task periodically re-runs saga.start for PLACED orders older
than a threshold.

Safety comes from Temporal, not from us: workflow ids are ord::{order_id}
with REJECT_DUPLICATE, and the port swallows AlreadyStarted — sweeping an
order whose saga is alive (or racing its own placement's start call) is a
no-op by construction. min_age exists to keep those no-op races rare, not
to prevent them; it only has to beat process-crash-and-restart timescales.

Runs in the API process ONLY (the same single-instance rule as the outbox
poller); the worker process executes workflows, it never starts them.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import OrderRepo

log = get_logger("order.sweeper")


class SagaStarter(Protocol):
    """The one verb the sweeper needs. Narrower than SagaPort on purpose —
    a healer that could also signal workflows would be a healer with too
    much authority (and a test fake with too much surface)."""

    async def start(self, order_id: str) -> bool: ...


class Sweeper:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        saga: SagaStarter,
        *,
        interval_seconds: float = 30.0,
        min_age_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._sessions = sessions
        self._saga = saga
        self._interval = interval_seconds
        self._min_age = min_age_seconds
        self._now = now

    async def run(self) -> None:
        """Resilient loop: one bad pass logs and retries next interval.
        Cancellation is the shutdown signal (mirrors OutboxPoller.run)."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("sweeper pass failed — will retry", error=str(exc))
            await asyncio.sleep(self._interval)

    async def sweep_once(self) -> int:
        """Start sagas for stale PLACED orders; returns how many NEW sagas
        were actually started. Per-order failures are logged and skipped so
        one unreachable start cannot shadow the rest of the backlog."""
        cutoff = self._now() - timedelta(seconds=self._min_age)
        async with self._sessions() as session:
            order_ids = await OrderRepo(session).stale_placed_order_ids(cutoff)
        swept = 0
        for order_id in order_ids:
            try:
                started = await self._saga.start(order_id)
            except Exception as exc:
                # Temporal down or mid-outage: the row stays PLACED, the
                # next pass retries — the to-do is durable by design.
                log.warning("sweep start failed — will retry", order_id=order_id, error=str(exc))
                continue
            if started:
                # A heal firing at all means a placement's own start call
                # died (crash or outage) — worth a loud line per order.
                log.warning("stale PLACED order swept — saga started", order_id=order_id)
                swept += 1
            else:
                # The workflow exists: either it is alive and merely slow
                # to move the row (worker backlog — not ours to fix), or it
                # CLOSED without ever transitioning (terminated/failed) and
                # REJECT_DUPLICATE will refuse forever — an ops case, never
                # a heal. Claiming "swept" here would mask both.
                log.info("sweep no-op — workflow already exists", order_id=order_id)
        return swept
