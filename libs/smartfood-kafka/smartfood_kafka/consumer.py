"""The shared consumer runtime — one at-least-once loop for every service.

Extracted from the character-identical loops identity and inventory each
hand-rolled (their own docstrings called this extraction out). The
at-least-once configuration (auto-commit OFF, commit AFTER handle) lives
HERE, in tested lib code, instead of inside per-service pragma-no-cover
wiring blocks.

New with the extraction is the failure policy neither copy had:

- The loop is SUPERVISED: a crashed pass (broker hiccup, commit failure,
  DLQ outage) logs and rejoins after a delay — mirroring OutboxPoller.run.
  Previously an unexpected exception killed the consumer task silently
  while the service kept serving HTTP. aiokafka clients are SINGLE-USE
  (start after stop asserts), so every pass builds fresh ones; injected
  test doubles are reused across passes, which is exactly how redelivery
  of uncommitted offsets looks to a handler anyway.
- A failing handler gets bounded in-process retries with exponential
  backoff (transient faults heal in seconds), then the ORIGINAL raw
  message is parked on `<source-topic>.dlq` with the failure stamped in
  headers, the offset is committed, and the partition keeps moving — a
  poison message costs seconds, not a partition. Only SerdeError parks at
  decode time (the BYTES are bad); a registry/transport failure there is
  OUR outage, so it crashes the pass and redelivers instead of draining
  good events to the DLQ.
- If the DLQ publish itself fails, nothing was committed: the pass
  crashes, the supervisor rejoins, and the message redelivers —
  at-least-once holds end to end.

Recovery is replay: DLQ values are byte-identical to the source message,
so re-producing them to the source topic re-runs the handler; consumer
dedupe (deterministic event ids) absorbs anything already applied.
"""

# pyright: reportMissingTypeStubs=false
# (aiokafka ships no py.typed; the Protocols below are our typed boundary.)

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from opentelemetry.context import Context
from opentelemetry.trace import INVALID_SPAN, SpanKind, Status, StatusCode
from prometheus_client import Counter
from smartfood_otel import REGISTRY, extract_context, get_logger, get_tracer, trace_id_of

from .serde import SerdeError

# One counter, one question per label pair: is this group healthy?
#   handled — the at-least-once contract delivered value
#   retried — transient handler failures (rate ~0 in a healthy system)
#   dlq     — parked forensically; ANY increase is an ops signal (ADR-0021)
#   crashed — a consume pass died and the supervisor rejoined
CONSUMER_EVENTS = Counter(
    "consumer_events_total",
    "Consumer outcomes by group.",
    labelnames=("group", "result"),
    registry=REGISTRY,
)

log = get_logger("smartfood-kafka.consumer")


class EventHandler(Protocol):
    """What a service registers: one decoded envelope in, side effects out.
    Raise to invoke the retry-then-DLQ policy; return to commit."""

    async def handle(self, event: dict[str, Any]) -> None: ...


class Decoder(Protocol):
    """Wire bytes → envelope dict. AvroSerde in production; tests swap a
    JSON stand-in so suites stay about handlers, not Avro. The contract:
    raise SerdeError for bad BYTES, anything else for a degraded decoder."""

    async def decode(self, data: bytes) -> dict[str, Any]: ...


class ConsumerClient(Protocol):
    """aiokafka-shaped consumer (injectable for tests). Messages carry
    .value/.key/.headers plus .topic/.partition/.offset (DLQ provenance)."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def commit(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[Any]: ...


class RawProducer(Protocol):
    """aiokafka-shaped producer for DLQ parking. Raw bytes on purpose:
    the parked value must stay byte-identical to the source message so
    replay is a verbatim re-produce, not a re-encode."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_and_wait(
        self, topic: str, value: bytes, *, key: bytes | None, headers: list[tuple[str, bytes]]
    ) -> Any: ...


def _bind_trace(message: Any) -> Context | None:
    # Rebind the producer's trace context from the Kafka headers: the
    # consumer's log lines join the SAME trace_id the original HTTP
    # request logged under, and the returned OTel context parents this
    # hop's CONSUMER span on the producing request's span. Failure-proof
    # on purpose: Kafka permits null/garbage header values, and a mangled
    # header must degrade to "no trace", never to a poison pill that
    # bypasses the retry/park boundary.
    structlog.contextvars.clear_contextvars()
    headers: list[tuple[str, Any]] = list(message.headers or [])
    traceparent = next(
        (v.decode(errors="replace") for k, v in headers if k == "traceparent" and v is not None),
        None,
    )
    if traceparent and (trace_id := trace_id_of(traceparent)):
        structlog.contextvars.bind_contextvars(trace_id=trace_id)
        return extract_context({"traceparent": traceparent})
    return None


class EventConsumer:
    def __init__(
        self,
        topics: str | Sequence[str],
        group: str,
        handler: EventHandler,
        serde: Decoder,
        *,
        bootstrap: str = "",
        client: ConsumerClient | None = None,
        dlq: RawProducer | None = None,
        max_attempts: int = 5,
        backoff_seconds: float = 0.5,
        restart_seconds: float = 5.0,
    ):
        self._topics = [topics] if isinstance(topics, str) else list(topics)
        self._group = group
        self._handler = handler
        self._serde = serde
        self._bootstrap = bootstrap
        self._injected_client = client
        self._injected_dlq = dlq
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._restart_seconds = restart_seconds

    def _make_client(self) -> ConsumerClient:
        """Fresh per pass — aiokafka clients cannot restart after stop().
        The at-least-once contract lives in one tested place: offsets move
        only by our explicit commit-after-handle, never behind our back."""
        if self._injected_client is not None:
            return self._injected_client
        return AIOKafkaConsumer(
            *self._topics,
            group_id=self._group,
            bootstrap_servers=self._bootstrap,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    def _make_dlq(self) -> RawProducer:
        if self._injected_dlq is not None:
            return self._injected_dlq
        return AIOKafkaProducer(bootstrap_servers=self._bootstrap)

    async def run(self) -> None:
        """Consume forever; cancellation is the shutdown signal. Like the
        outbox poller, a crashed pass never kills the task — nothing was
        committed for the in-flight message, so rejoining redelivers it."""
        while True:
            try:
                await self.consume_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                CONSUMER_EVENTS.labels(group=self._group, result="crashed").inc()
                log.error(
                    "consumer pass crashed — rejoining",
                    group=self._group,
                    error=str(exc),
                )
            await asyncio.sleep(self._restart_seconds)

    async def consume_once(self) -> None:
        """One client session: start → decode/handle/commit each message →
        stop. Returns only when the client's iterator ends (test stubs; a
        live client iterates forever)."""
        dlq = self._make_dlq()
        await dlq.start()
        try:
            client = self._make_client()
            await client.start()
            try:
                async for message in client:
                    await self._process(message, dlq)
                    await client.commit()
            finally:
                await client.stop()
        finally:
            await dlq.stop()

    async def _process(self, message: Any, dlq: RawProducer) -> None:
        ctx = _bind_trace(message)
        # The async hop as a CONSUMER span, child of the producing request.
        # No header = no span (nothing to join); handler work INSIDE the
        # span inherits it, so anything the handler stages chains onward.
        span_cm = (
            get_tracer().start_as_current_span(
                f"consume {', '.join(self._topics)}",
                context=ctx,
                kind=SpanKind.CONSUMER,
                attributes={"messaging.consumer.group": self._group},
            )
            if ctx is not None
            else nullcontext(INVALID_SPAN)
        )
        with span_cm as span:

            def mark(result: str) -> None:
                # TOTAL by construction: a tracing bug in here once parked
                # a healthy message to the DLQ during development.
                if span.is_recording():
                    span.set_attribute("result", result)
                    if result == "dlq":
                        span.set_status(Status(StatusCode.ERROR))

            try:
                event = await self._serde.decode(message.value)
            except SerdeError as exc:
                # The bytes themselves are bad — no retry can fix them. Any
                # OTHER decode failure (registry down, transport) propagates:
                # crash the pass, rejoin, redeliver.
                await self._park(message, dlq, exc, attempts=0)
                mark("dlq")
                return
            for attempt in range(1, self._max_attempts + 1):
                try:
                    await self._handler.handle(event)
                    CONSUMER_EVENTS.labels(group=self._group, result="handled").inc()
                    mark("handled")
                    return
                except Exception as exc:
                    if attempt == self._max_attempts:
                        await self._park(message, dlq, exc, attempts=attempt)
                        mark("dlq")
                        return
                    CONSUMER_EVENTS.labels(group=self._group, result="retried").inc()
                    log.warning(
                        "handler failed — retrying",
                        group=self._group,
                        event_id=str(event.get("event_id")),
                        attempt=attempt,
                        error=str(exc),
                    )
                    await asyncio.sleep(self._backoff_seconds * 2 ** (attempt - 1))

    async def _park(
        self, message: Any, dlq: RawProducer, error: BaseException, *, attempts: int
    ) -> None:
        """Park the ORIGINAL bytes on `<source-topic>.dlq`, stamped with why:
        ops inspect the failure in the console and replay verbatim."""
        dlq_topic = f"{message.topic}.dlq"
        headers = list(message.headers or []) + [
            ("dlq.error.type", type(error).__name__.encode()),
            ("dlq.error.message", str(error)[:500].encode()),
            ("dlq.source.topic", str(message.topic).encode()),
            ("dlq.source.partition", str(message.partition).encode()),
            ("dlq.source.offset", str(message.offset).encode()),
            ("dlq.attempts", str(attempts).encode()),
            ("dlq.failed_at", datetime.now(UTC).isoformat().encode()),
        ]
        await dlq.send_and_wait(dlq_topic, message.value, key=message.key, headers=headers)
        CONSUMER_EVENTS.labels(group=self._group, result="dlq").inc()
        log.error(
            "event parked to DLQ — needs ops replay",
            group=self._group,
            dlq_topic=dlq_topic,
            source_offset=message.offset,
            error_type=type(error).__name__,
            error=str(error)[:500],
            attempts=attempts,
        )
