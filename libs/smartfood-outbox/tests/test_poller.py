"""Every poller branch: ordered drain, marking, batching, traceparent
headers, at-least-once on failure, and the resilient run loop."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from smartfood_outbox import OutboxPoller, event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

metadata = sa.MetaData()
outbox = sa.Table(  # the column contract every service's outbox satisfies
    "outbox",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("aggregate_type", sa.Text, nullable=False),
    sa.Column("aggregate_id", sa.Text, nullable=False),
    sa.Column("aggregate_version", sa.Integer, nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("traceparent", sa.Text, nullable=True),
)


class StubProducer:
    def __init__(self, fail_times: int = 0):
        self.sent: list[dict] = []
        self.fail_times = fail_times

    async def send(self, topic, *, subject, schema, key, record, headers=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("kafka down")
        self.sent.append({"topic": topic, "key": key, "record": record, "headers": headers})


async def _sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _stage(sessions, n: int, *, traceparent: str | None = None):
    base = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    async with sessions() as s:
        for i in range(n):
            await s.execute(
                outbox.insert().values(
                    id=event_id("restaurant", "rst_1", i + 1, "ItemAdded"),
                    aggregate_type="restaurant",
                    aggregate_id="rst_1",
                    aggregate_version=i + 1,
                    event_type="ItemAdded",
                    payload={"n": i},
                    occurred_at=base + timedelta(seconds=i),
                    traceparent=traceparent,
                )
            )
        await s.commit()


async def _unpublished(sessions) -> int:
    async with sessions() as s:
        return (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(outbox.c.published_at.is_(None))
            )
        ).scalar_one()


async def test_drain_publishes_in_order_and_marks():
    sessions = await _sessions()
    await _stage(sessions, 3, traceparent="00-abc-def-01")
    producer = StubProducer()
    poller = OutboxPoller(
        sessions,
        outbox,
        topic="c1.catalog.changes",
        producer=producer,
    )
    assert await poller.drain_once() == 3
    versions = [s["record"]["aggregate_version"] for s in producer.sent]
    assert versions == [1, 2, 3]  # occurred_at order — per-aggregate ordering
    first = producer.sent[0]
    assert first["key"] == "rst_1"
    assert first["record"]["cell_id"] == "c1"
    assert json.loads(first["record"]["payload"]) == {"n": 0}
    assert first["record"]["occurred_at"].tzinfo is not None  # aware for Avro
    assert first["headers"] == [("traceparent", b"00-abc-def-01")]
    assert await _unpublished(sessions) == 0
    assert await poller.drain_once() == 0  # nothing left


async def test_no_traceparent_means_no_header():
    sessions = await _sessions()
    await _stage(sessions, 1)  # traceparent NULL
    producer = StubProducer()
    poller = OutboxPoller(sessions, outbox, topic="t", producer=producer)
    await poller.drain_once()
    assert producer.sent[0]["headers"] == []


async def test_publish_log_line_carries_the_rows_trace_id(capsys):
    """The line that lets `grep trace_id` cross the async hop."""
    from smartfood_otel import setup_logging

    sessions = await _sessions()
    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"  # valid W3C, unlike above
    await _stage(sessions, 1, traceparent=tp)
    setup_logging("outbox-test")
    poller = OutboxPoller(sessions, outbox, topic="t", producer=StubProducer())
    await poller.drain_once()
    published = [
        parsed
        for line in capsys.readouterr().out.strip().splitlines()
        if line.startswith("{") and (parsed := json.loads(line))
        if parsed.get("event") == "outbox event published"
    ]
    assert published[0]["trace_id"] == "ab" * 16
    assert published[0]["event_type"] == "ItemAdded"


async def test_batch_size_bounds_each_pass():
    sessions = await _sessions()
    await _stage(sessions, 5)
    producer = StubProducer()
    poller = OutboxPoller(
        sessions,
        outbox,
        topic="t",
        producer=producer,
        batch_size=2,
    )
    assert await poller.drain_once() == 2
    assert await _unpublished(sessions) == 3


async def test_failed_send_marks_nothing():
    """At-least-once: the batch is marked only after EVERY send confirmed."""
    sessions = await _sessions()
    await _stage(sessions, 2)
    producer = StubProducer(fail_times=1)
    poller = OutboxPoller(sessions, outbox, topic="t", producer=producer)
    with pytest.raises(RuntimeError):
        await poller.drain_once()
    assert await _unpublished(sessions) == 2  # everything still queued


async def test_run_survives_failures_and_cancels_cleanly():
    sessions = await _sessions()
    await _stage(sessions, 1)
    producer = StubProducer(fail_times=1)  # first pass dies, second succeeds
    poller = OutboxPoller(
        sessions,
        outbox,
        topic="t",
        producer=producer,
        interval=0.01,
    )
    task = asyncio.create_task(poller.run())
    for _ in range(200):
        if producer.sent:
            break
        await asyncio.sleep(0.01)
    assert producer.sent  # recovered after the failed pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancellation_mid_drain_propagates():
    """Shutdown during an in-flight send must cancel, not be swallowed by
    the retry-on-error handler."""
    sessions = await _sessions()
    await _stage(sessions, 1)
    entered = asyncio.Event()

    class BlockingProducer:
        async def send(self, topic, **kwargs):
            entered.set()
            await asyncio.sleep(3600)

    poller = OutboxPoller(
        sessions,
        outbox,
        topic="t",
        producer=BlockingProducer(),
    )
    task = asyncio.create_task(poller.run())
    await entered.wait()  # cancellation lands INSIDE drain_once
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _unpublished(sessions) == 1  # nothing was marked


# ── metrics: what NFR-6's lag SLO is measured with ─────────────────


async def test_drain_records_lag_published_and_backlog():
    from smartfood_otel import REGISTRY

    def sample(name):
        return REGISTRY.get_sample_value(name) or 0.0

    published_before = sample("outbox_published_total")
    lag_count_before = sample("outbox_publish_lag_seconds_count")

    sessions = await _sessions()
    await _stage(sessions, 3)
    producer = StubProducer()
    poller = OutboxPoller(
        sessions, outbox, topic="t", producer=producer, cell_id="c1", batch_size=2
    )

    assert await poller.drain_once() == 2  # first batch: 2 published, 1 left
    assert sample("outbox_published_total") == published_before + 2
    assert sample("outbox_publish_lag_seconds_count") == lag_count_before + 2
    assert sample("outbox_pending") == 1.0  # the gauge is the batch's leftover

    assert await poller.drain_once() == 1
    assert sample("outbox_pending") == 0.0
