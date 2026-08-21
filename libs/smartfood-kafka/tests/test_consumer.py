"""Every branch of the consumer runtime, offline: the at-least-once loop,
trace rebinding, bounded retry, DLQ parking, and supervision."""

import asyncio
import json
from typing import Any

import pytest
import structlog
from smartfood_kafka import EventConsumer
from smartfood_kafka.testing import (
    RecordingHandler,
    StubDlq,
    StubKafkaConsumer,
    StubMessage,
    StubSerde,
)


def _msg(event_id: str, **kwargs: Any) -> StubMessage:
    return StubMessage(value=json.dumps({"event_id": event_id}).encode(), **kwargs)


def _consumer(
    client: StubKafkaConsumer,
    handler: Any,
    dlq: StubDlq,
    **tuning: Any,
) -> EventConsumer:
    tuning.setdefault("backoff_seconds", 0.0)
    tuning.setdefault("restart_seconds", 0.0)
    return EventConsumer(
        "c1.test.events", "test.group", handler, StubSerde(), client=client, dlq=dlq, **tuning
    )


async def _until(condition: Any, deadline: float = 2.0) -> None:
    async with asyncio.timeout(deadline):
        # Polling stub state set by a background task — there is no event
        # to await, and sleep(0) just yields the loop.
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def test_consume_once_decodes_handles_then_commits():
    client = StubKafkaConsumer([_msg("e1"), _msg("e2")])
    handler = RecordingHandler()
    dlq = StubDlq()
    await _consumer(client, handler, dlq).consume_once()
    assert [e["event_id"] for e in handler.events] == ["e1", "e2"]
    assert client.commits == 2  # committed AFTER each handle — at-least-once
    assert client.starts == 1 and client.stopped  # stop() even on normal exit
    assert dlq.started and dlq.stopped and dlq.parked == []


async def test_trace_context_rebinds_per_message():
    """Valid header binds the producing request's trace_id; absent or
    garbage headers bind nothing — and never leak the previous message's."""
    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    client = StubKafkaConsumer(
        [
            _msg("e1", headers=[("traceparent", tp.encode())]),
            _msg("e2"),  # no headers at all
            _msg("e3", headers=[("traceparent", b"garbage")]),
        ]
    )
    seen: list[str | None] = []

    class ContextProbe:
        async def handle(self, event: dict[str, Any]) -> None:
            seen.append(structlog.contextvars.get_contextvars().get("trace_id"))

    await _consumer(client, ContextProbe(), StubDlq()).consume_once()
    assert seen == ["ab" * 16, None, None]


async def test_transient_handler_failure_retries_then_succeeds():
    client = StubKafkaConsumer([_msg("e1")])
    handler = RecordingHandler(fail_times=2)
    dlq = StubDlq()
    await _consumer(client, handler, dlq).consume_once()
    assert handler.attempts == 3  # two failures absorbed in-process
    assert [e["event_id"] for e in handler.events] == ["e1"]
    assert client.commits == 1 and dlq.parked == []


async def test_retry_backoff_is_exponential(monkeypatch):
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    client = StubKafkaConsumer([_msg("e1")])
    handler = RecordingHandler(fail_times=3)
    await _consumer(client, handler, StubDlq(), backoff_seconds=0.5).consume_once()
    assert delays == [0.5, 1.0, 2.0]  # doubling per attempt
    assert handler.events  # and the 4th attempt succeeded


async def test_exhausted_retries_park_to_dlq_and_move_on():
    poison = _msg("bad", topic="c1.orders.events", partition=3, offset=42, key=b"ord_1")
    client = StubKafkaConsumer([poison, _msg("good")])
    handler = RecordingHandler(fail_times=2)
    dlq = StubDlq()
    await _consumer(client, handler, dlq, max_attempts=2).consume_once()

    (topic, value, key, headers), *rest = dlq.parked
    assert rest == []
    assert topic == "c1.orders.events.dlq"  # derived from the SOURCE topic
    assert value == poison.value and key == b"ord_1"  # byte-identical for replay
    stamped = {k: v for k, v in headers}
    assert stamped["dlq.error.type"] == b"RuntimeError"
    assert stamped["dlq.error.message"] == b"handler boom"
    assert stamped["dlq.source.topic"] == b"c1.orders.events"
    assert stamped["dlq.source.partition"] == b"3"
    assert stamped["dlq.source.offset"] == b"42"
    assert stamped["dlq.attempts"] == b"2"
    assert b"T" in stamped["dlq.failed_at"]  # ISO timestamp

    # The partition kept moving: the poison offset was committed and the
    # NEXT message was handled normally.
    assert client.commits == 2
    assert [e["event_id"] for e in handler.events] == ["good"]


async def test_undecodable_message_parks_immediately():
    """Bad BYTES (SerdeError) get zero handler attempts — no retry can
    fix them."""
    garbage = StubMessage(value=b"not json", headers=[("traceparent", b"x")])
    client = StubKafkaConsumer([garbage])
    handler = RecordingHandler()
    dlq = StubDlq()
    await _consumer(client, handler, dlq).consume_once()
    assert handler.attempts == 0
    (topic, value, _, headers) = dlq.parked[0]
    assert topic == "c1.test.events.dlq" and value == b"not json"
    stamped = dict(headers)
    assert stamped["dlq.attempts"] == b"0"
    assert ("traceparent", b"x") in headers  # original headers preserved
    assert client.commits == 1


async def test_degraded_decoder_crashes_the_pass_instead_of_parking():
    """A registry/transport failure during decode means WE are degraded,
    not the message: nothing may be parked or committed — the pass
    crashes, the supervisor rejoins, and the GOOD event redelivers."""

    class RegistryDown:
        async def decode(self, data: bytes) -> dict[str, Any]:
            raise RuntimeError("schema registry unreachable")

    client = StubKafkaConsumer([_msg("e1")])
    dlq = StubDlq()
    consumer = EventConsumer(
        "c1.test.events", "test.group", RecordingHandler(), RegistryDown(), client=client, dlq=dlq
    )
    with pytest.raises(RuntimeError, match="registry unreachable"):
        await consumer.consume_once()
    assert dlq.parked == [] and client.commits == 0


async def test_null_or_mangled_trace_header_is_not_a_poison_pill():
    """Kafka permits null header values; a mangled traceparent must
    degrade to 'no trace' and the message must still be handled."""
    client = StubKafkaConsumer(
        [
            _msg("e1", headers=[("traceparent", None)]),
            _msg("e2", headers=[("traceparent", b"\xff\xfe garbage")]),
        ]
    )
    handler = RecordingHandler()
    dlq = StubDlq()
    await _consumer(client, handler, dlq).consume_once()
    assert [e["event_id"] for e in handler.events] == ["e1", "e2"]
    assert dlq.parked == []


async def test_dlq_outage_crashes_the_pass_without_committing():
    """If parking fails, the offset must stay uncommitted so the message
    redelivers — losing it silently is the one unacceptable outcome."""
    client = StubKafkaConsumer([_msg("e1")])
    handler = RecordingHandler(fail_times=99)
    dlq = StubDlq(fail_times=1)
    with pytest.raises(RuntimeError, match="dlq down"):
        await _consumer(client, handler, dlq, max_attempts=1).consume_once()
    assert client.commits == 0
    assert client.stopped and dlq.stopped  # both cleaned up on the way out


async def test_run_supervises_a_crashed_pass_and_recovers():
    """The silent-death regression: a crashed pass (DLQ down here) must
    rejoin, redeliver, and eventually park — never die unobserved."""
    client = StubKafkaConsumer([_msg("e1")])
    handler = RecordingHandler(fail_times=99)
    dlq = StubDlq(fail_times=1)
    task = asyncio.create_task(_consumer(client, handler, dlq, max_attempts=1).run())
    await _until(lambda: dlq.parked)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.starts >= 2  # first pass crashed, second rejoined and parked


async def test_commit_crash_redelivers_the_handled_message():
    """At-least-once made visible: a commit failure after a successful
    handle means the same event is handled AGAIN on the next pass — the
    reason every handler must be idempotent."""
    client = StubKafkaConsumer([_msg("e1")])
    client.fail_commits = 1
    handler = RecordingHandler()
    task = asyncio.create_task(_consumer(client, handler, StubDlq()).run())
    await _until(lambda: len(handler.events) >= 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["event_id"] for e in handler.events][:2] == ["e1", "e1"]


async def test_run_rejoins_after_a_normal_pass_end():
    """A client iterator ending (stub here, closed client live) is not
    death either — run() loops back in."""
    client = StubKafkaConsumer([_msg("e1")])
    handler = RecordingHandler()
    task = asyncio.create_task(_consumer(client, handler, StubDlq()).run())
    await _until(lambda: client.starts >= 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancellation_propagates_and_stops_clients():
    class BlockingConsumer(StubKafkaConsumer):
        def __aiter__(self) -> Any:
            async def gen() -> Any:
                await asyncio.Event().wait()  # block until cancelled
                yield  # pragma: no cover — unreachable

            return gen()

    client = BlockingConsumer([])
    dlq = StubDlq()
    task = asyncio.create_task(_consumer(client, RecordingHandler(), dlq).run())
    await _until(lambda: client.starts == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.stopped and dlq.stopped


async def test_live_construction_owns_the_at_least_once_config():
    """No client injected → the runtime builds the real aiokafka pair
    itself (single topic or several) — the config block that used to live
    in per-service pragma-no-cover wiring, now constructable in a test.
    Pinned: auto-commit OFF, earliest reset, the caller's group id."""
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    from smartfood_kafka import AvroSerde, SchemaRegistry

    serde = AvroSerde(SchemaRegistry("http://sr.test"))
    single = EventConsumer("c1.a.events", "g", RecordingHandler(), serde, bootstrap="k:9092")
    many = EventConsumer(
        ["c1.a.events", "c1.b.events"], "g", RecordingHandler(), serde, bootstrap="k:9092"
    )
    for consumer in (single, many):
        client = consumer._make_client()
        assert isinstance(client, AIOKafkaConsumer)
        assert isinstance(consumer._make_dlq(), AIOKafkaProducer)
        assert client._enable_auto_commit is False
        assert client._auto_offset_reset == "earliest"
        assert client._group_id == "g"
        # aiokafka clients are single-use (start after stop asserts), so
        # the supervisor must get a FRESH pair every pass.
        assert consumer._make_client() is not client


async def test_injected_doubles_are_reused_across_passes():
    """The test seam: injected stubs come back identically each pass —
    which is how the supervision tests above see starts >= 2."""
    client = StubKafkaConsumer([])
    dlq = StubDlq()
    consumer = _consumer(client, RecordingHandler(), dlq)
    assert consumer._make_client() is client and consumer._make_dlq() is dlq


# ── metrics: the counters ops alerts key on (ADR-0021) ─────────────


def _count(result: str) -> float:
    from smartfood_otel import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "consumer_events_total", {"group": "test.group", "result": result}
        )
        or 0.0
    )


async def test_metrics_count_handled_retried_and_dlq():
    """One flow, three counters: two failures then success = 2 retried +
    1 handled; a poison message = 1 dlq. Deltas, not absolutes — the
    registry is shared across the suite."""
    before = {r: _count(r) for r in ("handled", "retried", "dlq")}

    client = StubKafkaConsumer([_msg("e1")])
    await _consumer(client, RecordingHandler(fail_times=2), StubDlq()).consume_once()

    client = StubKafkaConsumer([_msg("e2")])
    dlq = StubDlq()
    await _consumer(client, RecordingHandler(fail_times=99), dlq, max_attempts=2).consume_once()

    assert _count("handled") == before["handled"] + 1
    assert _count("retried") == before["retried"] + 3  # 2 pre-success + 1 pre-park
    assert _count("dlq") == before["dlq"] + 1
    assert len(dlq.parked) == 1


async def test_metrics_count_crashed_passes():
    before = _count("crashed")

    class ExplodingClient(StubKafkaConsumer):
        async def start(self):
            raise RuntimeError("broker gone")

    consumer = _consumer(ExplodingClient([]), RecordingHandler(), StubDlq())
    task = asyncio.create_task(consumer.run())
    await _until(lambda: _count("crashed") > before)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _count("crashed") >= before + 1


async def test_consumer_exports_a_child_span_of_the_producing_request():
    """The async hop as a trace edge: the consumer span's parent is the
    span that produced the message (via the Kafka traceparent header)."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind
    from smartfood_otel import (
        make_traceparent,
        reset_tracing,
        setup_tracing,
        span_id_of,
        trace_id_of,
    )

    exporter = InMemorySpanExporter()
    setup_tracing("test-consumer", "", exporter=exporter)
    try:
        producer_tp = make_traceparent()
        client = StubKafkaConsumer([_msg("e1", headers=[("traceparent", producer_tp.encode())])])
        await _consumer(client, RecordingHandler(), StubDlq()).consume_once()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.context is not None
        assert span.name == "consume c1.test.events"
        assert format(span.context.trace_id, "032x") == trace_id_of(producer_tp)
        assert span.parent is not None
        assert format(span.parent.span_id, "016x") == span_id_of(producer_tp)
        assert span.kind == SpanKind.CONSUMER
        assert span.attributes is not None and span.attributes["result"] == "handled"

        # And the sad path: a poison message's span is marked result=dlq
        # with ERROR status — the trace-level twin of the metrics counter.
        from opentelemetry.trace import StatusCode

        poison = StubKafkaConsumer(
            [_msg("e2", headers=[("traceparent", make_traceparent().encode())])]
        )
        await _consumer(
            poison, RecordingHandler(fail_times=99), StubDlq(), max_attempts=1
        ).consume_once()
        dlq_span = exporter.get_finished_spans()[-1]
        assert dlq_span.attributes is not None and dlq_span.attributes["result"] == "dlq"
        assert dlq_span.status.status_code == StatusCode.ERROR
    finally:
        reset_tracing()
