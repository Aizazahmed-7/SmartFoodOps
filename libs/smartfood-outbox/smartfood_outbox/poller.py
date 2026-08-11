"""The dev-mode outbox drain (ADR-0012: OUTBOX_MODE=poller).

Generic over any service's outbox table (the columns are the contract:
id, aggregate_type, aggregate_id, aggregate_version, event_type, payload,
occurred_at, published_at, traceparent). Guarantees:

- at-least-once: rows are published THEN marked; a crash between the two
  re-sends on the next pass — consumers dedupe by the deterministic event_id.
- per-aggregate ordering: rows drain in (occurred_at, id) order, keyed by
  aggregate_id, WITH ONE POLLER INSTANCE per service (dev reality). Multiple
  instances + SKIP LOCKED could interleave an aggregate's rows; the prod
  path (Debezium, W3) reads the WAL and has no such caveat.

Week 3 adds Debezium mode; both emit the same wire format via smartfood-kafka.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from smartfood_kafka import DOMAIN_EVENT_SCHEMA, DOMAIN_EVENT_SUBJECT, EventProducer
from smartfood_otel import get_logger
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = get_logger("smartfood-outbox")


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; Avro timestamp-micros wants aware."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _record(row: Row[Any], cell_id: str) -> dict[str, Any]:
    return {
        "event_id": row.id,
        "event_type": row.event_type,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "aggregate_version": row.aggregate_version,
        "occurred_at": _aware(row.occurred_at),
        "cell_id": cell_id,
        "payload": json.dumps(row.payload),
    }


class OutboxPoller:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        table: sa.Table,
        *,
        topic: str,
        producer: EventProducer,
        cell_id: str = "c1",
        batch_size: int = 100,
        interval: float = 0.5,
    ):
        self._sessions = sessions
        self._table = table
        self._topic = topic
        self._producer = producer
        self._cell_id = cell_id
        self._batch_size = batch_size
        self._interval = interval

    async def drain_once(self) -> int:
        """Publish one batch of unpublished rows; returns how many."""
        table = self._table
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.select(table)
                    .where(table.c.published_at.is_(None))
                    .order_by(table.c.occurred_at, table.c.id)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return 0
            for row in rows:
                traceparent = getattr(row, "traceparent", None)
                await self._producer.send(
                    self._topic,
                    subject=DOMAIN_EVENT_SUBJECT,
                    schema=DOMAIN_EVENT_SCHEMA,
                    key=row.aggregate_id,
                    record=_record(row, self._cell_id),
                    headers=(
                        [("traceparent", traceparent.encode())] if traceparent else []
                    ),
                )
            # Mark only after EVERY send in the batch was broker-confirmed —
            # a crash mid-batch re-sends the whole batch (dedupe absorbs it).
            await session.execute(
                table.update()
                .where(table.c.id.in_([row.id for row in rows]))
                .values(published_at=datetime.now(UTC))
            )
            await session.commit()
            return len(rows)

    async def run(self) -> None:
        """Drain forever; cancellation is the shutdown signal. A failing
        drain (Kafka/registry/DB down) must never kill the task — nothing
        was marked published, so the next pass simply re-sends."""
        while True:
            try:
                drained = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("outbox drain failed — will retry", error=str(exc))
                drained = 0
            if drained == 0:
                await asyncio.sleep(self._interval)
