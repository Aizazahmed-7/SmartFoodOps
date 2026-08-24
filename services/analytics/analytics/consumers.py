"""The facts projector — analytics' only write path.

ONE micro-batched consumer on c1.orders.events (smartfood_kafka's
run_batches, FR-43): a poll's worth of events folds in ONE transaction with
ONE offset commit. Idempotency is structural — apply_event writes absolute
values keyed by order_id, so a redelivered batch (crash before commit, or
the batch runtime's degrade-to-singles pass) converges instead of
double-counting. That structural property is WHY this service may batch at
all: the batch runtime's contract is "handler writes must be idempotent".

No payments loop (unlike notification): every metric here derives from
order events alone, which carry totals and all lifecycle timestamps.
"""

import json
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import AnalyticsRepo

log = get_logger("analytics.projector")

GROUP_FACTS = "analytics.facts.orders"


class FactsProjector:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        """Per-message mode compatibility (EventHandler): a batch of one.
        Batch mode is the production path; this keeps the projector usable
        under either loop and satisfies the constructor's protocol."""
        await self.handle_batch([event])

    async def handle_batch(self, events: list[dict[str, Any]]) -> None:
        async with self._sessions() as session:
            repo = AnalyticsRepo(session)
            for event in events:
                # The Avro envelope carries payload as a JSON STRING (the
                # schema stays stable while payload shapes evolve). Caught
                # live: indexing it like a dict parked the entire topic
                # history — the batch runtime degraded to singles and DLQ'd
                # every event, exactly as designed, while this line was
                # wrong. Unparseable payloads raise → retry → park, with
                # forensics; that is the correct fate for a truly bad one.
                raw = event.get("payload") or {}
                payload: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
                await repo.apply_event(str(event.get("event_type", "")), payload)
            await session.commit()
        log.info("facts folded", events=len(events))
