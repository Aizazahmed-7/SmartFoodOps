"""OpenTelemetry wiring — the provider, the OTLP exporter, and the tracer
everything in this platform records with.

This replaced a hand-rolled OTLP/JSON exporter (git history: the first
tracing slice). Same wire format, same Jaeger, same setup_tracing()
signature at every call site — but spans are now made by the standard SDK,
which buys what the hand-rolled version could not: the ecosystem
(auto-instrumentation for httpx/SQLAlchemy, Temporal's TracingInterceptor)
and maintained batching/backpressure (BatchSpanProcessor drops when full —
the same never-hurt-the-request philosophy we wrote by hand).

The provider is MODULE state, not only the OTel global: get_tracer() reads
our own reference, so tests can install an InMemorySpanExporter no matter
what the process-global was set to earlier. The global is still set (best
effort) so future auto-instrumentation finds it. Unconfigured, get_tracer()
hands back the API's no-op tracer — spans cost nothing and export nowhere,
which is every unit test and any environment without a collector.
"""

from contextlib import suppress

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from .metrics import TRACING_ARMED


class _DropParentlessInfraSpans(SpanProcessor):
    """Drop ROOT spans minted by infra auto-instrumentation: the outbox
    poller's idle SELECT would otherwise export a one-span trace per pass
    per service — thousands a day of pure noise. A parentless SQL span has
    no request behind it by definition; SQL spans WITH parents (the ones
    that explain a request) pass through untouched."""

    def __init__(self, delegate: SpanProcessor):
        self._delegate = delegate

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._delegate.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        scope = span.instrumentation_scope
        if span.parent is None and scope is not None and "sqlalchemy" in scope.name:
            return
        self._delegate.on_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


_provider: TracerProvider | None = None
_instrumented = False


def _instrument(provider: TracerProvider) -> None:
    """Arm auto-instrumentation: every httpx call becomes a CLIENT span,
    every SQLAlchemy query a span under whatever is current. Must run
    BEFORE engines/clients are created (setup_tracing is called right after
    setup_logging, ahead of create_async_engine, in every create_app).
    Process-global monkeypatches, so guarded and reversible for tests."""
    global _instrumented
    if _instrumented:
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
        SQLAlchemyInstrumentor,
    )

    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    SQLAlchemyInstrumentor().instrument(tracer_provider=provider)
    _instrumented = True


def _uninstrument() -> None:
    global _instrumented
    if not _instrumented:
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
        SQLAlchemyInstrumentor,
    )

    HTTPXClientInstrumentor().uninstrument()
    SQLAlchemyInstrumentor().uninstrument()
    _instrumented = False


def setup_tracing(service: str, endpoint: str, *, exporter: SpanExporter | None = None) -> None:
    """Arm span export. Empty endpoint and no exporter = stay off (the
    default everywhere without a collector). `exporter` is the test seam —
    it gets a SimpleSpanProcessor so spans land synchronously and
    deterministically; the live OTLP path batches."""
    global _provider
    # Report BEFORE the early return: a process with tracing off must still
    # say so out loud (that is the whole point of the gauge).
    TRACING_ARMED.set(0)
    if exporter is None:
        if not endpoint:
            return
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
        processor: BatchSpanProcessor | SimpleSpanProcessor = BatchSpanProcessor(exporter)
    else:
        processor = SimpleSpanProcessor(exporter)
    _provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    _provider.add_span_processor(_DropParentlessInfraSpans(processor))
    _instrument(_provider)
    with suppress(Exception):  # already set by an earlier setup in-process: ours still wins
        trace.set_tracer_provider(_provider)
    TRACING_ARMED.set(1)


def get_tracer() -> trace.Tracer:
    """The tracer our middleware/consumer/poller record with. No-op when
    tracing is unconfigured — callers never check."""
    if _provider is not None:
        return _provider.get_tracer("smartfood-otel")
    return trace.NoOpTracer()


def reset_tracing() -> None:
    """Test hook: flush and disarm."""
    global _provider
    _uninstrument()
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    TRACING_ARMED.set(0)
