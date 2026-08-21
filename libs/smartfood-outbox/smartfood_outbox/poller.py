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
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from opentelemetry.trace import SpanKind
from prometheus_client import Counter, Gauge, Histogram
from smartfood_kafka import DOMAIN_EVENT_SCHEMA, DOMAIN_EVENT_SUBJECT
from smartfood_otel import REGISTRY, extract_context, get_logger, get_tracer, trace_id_of
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = get_logger("smartfood-outbox")

# NFR-6 asserts publish lag p99 < 5s — these are what measure it. Lag is
# observed at PUBLISH time (now − occurred_at), so a stalled poller shows
# up as a growing gauge first, then as a lag spike when it recovers.
OUTBOX_PUBLISHED = Counter("outbox_published_total", "Events drained to Kafka.", registry=REGISTRY)
OUTBOX_LAG = Histogram(
    "outbox_publish_lag_seconds",
    "Staged→published latency per event.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0),
    registry=REGISTRY,
)
OUTBOX_PENDING = Gauge("outbox_pending", "Rows staged but not yet published.", registry=REGISTRY)


class DomainEventSender(Protocol):
    """The one capability the poller needs — structural, so tests can hand
    in a stub without inheriting from the real EventProducer."""

    async def send(
        self,
        topic: str,
        *,
        subject: str,
        schema: dict[str, Any],
        key: str,
        record: dict[str, Any],
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None: ...


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
        producer: DomainEventSender,
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
            # Backlog gauge FIRST, success or not: if Kafka is fully down,
            # this pass will raise at send — but the gauge already told the
            # truth, so OutboxBacklogGrowing fires. A gauge only set on
            # success goes blind exactly when the outbox is most broken
            # (the same lesson as `up`: one signal per failure that does
            # not depend on the failing thing succeeding).
            backlog = (
                await session.execute(
                    sa.select(sa.func.count()).where(table.c.published_at.is_(None))
                )
            ).scalar_one()
            OUTBOX_PENDING.set(backlog)
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
                # The async hop's PRODUCER span, parented on the request
                # that STAGED the row (its traceparent was stored in the
                # column). No traceparent = plain send, no span.
                span_cm = (
                    get_tracer().start_as_current_span(
                        f"outbox publish {row.event_type}",
                        context=extract_context({"traceparent": traceparent}),
                        kind=SpanKind.PRODUCER,
                        attributes={"messaging.destination": self._topic},
                    )
                    if traceparent
                    else nullcontext(None)
                )
                with span_cm:
                    await self._producer.send(
                        self._topic,
                        subject=DOMAIN_EVENT_SUBJECT,
                        schema=DOMAIN_EVENT_SCHEMA,
                        key=row.aggregate_id,
                        record=_record(row, self._cell_id),
                        headers=([("traceparent", traceparent.encode())] if traceparent else []),
                    )
                # The poller runs outside any request, so the row's stored
                # traceparent is stamped explicitly — this line is what makes
                # `grep trace_id` cross the async hop.
                trace_id = trace_id_of(traceparent) if traceparent else None
                log.info(
                    "outbox event published",
                    event_id=row.id,
                    event_type=row.event_type,
                    topic=self._topic,
                    **({"trace_id": trace_id} if trace_id else {}),
                )
            # Mark only after EVERY send in the batch was broker-confirmed —
            # a crash mid-batch re-sends the whole batch (dedupe absorbs it).
            now = datetime.now(UTC)
            for row in rows:
                OUTBOX_LAG.observe((now - _aware(row.occurred_at)).total_seconds())
            OUTBOX_PUBLISHED.inc(len(rows))
            await session.execute(
                table.update()
                .where(table.c.id.in_([row.id for row in rows]))
                .values(published_at=now)
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
