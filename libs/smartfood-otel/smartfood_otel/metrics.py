"""Prometheus metrics — the instruments and the /metrics exposition body.

Framework-free by the same rule as the middleware (see its docstring): this
module owns the registry and the recording helpers; smartfood-api mounts the
HTTP route. The RequestContextMiddleware calls observe_request() once per
request from the hook that ALREADY computes method/status/duration, so HTTP
latency and error rate come for free — no per-handler instrumentation.

A DEDICATED registry, not prometheus_client's global default: a service's
/metrics then shows exactly what it declared, with no stray interpreter or
GC gauges, and tests read a clean surface.

No `service` label on purpose — the scrape target (Prometheus job/instance)
already identifies which service a series came from, so putting the name in
the metric too would just be a redundant, higher-cardinality copy.
"""

from typing import cast

from opentelemetry import trace as _trace
from prometheus_client import CollectorRegistry, Gauge, Histogram, start_http_server

# The openmetrics exposition module ships no type information — alias it
# and cast at the two touch points instead of ignoring per-symbol.
from prometheus_client.openmetrics import exposition as _openmetrics

REGISTRY = CollectorRegistry()

# Buckets tuned to the SLOs the ADRs assert (placement p95 < 3s, p99 < 6s):
# dense below 3s where the decisions and the SLO boundaries live, a long tail
# for the pathological. Prometheus reads p95/p99 off these boundaries.
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency, labelled by method and response status.",
    labelnames=("method", "status"),
    buckets=_BUCKETS,
    registry=REGISTRY,
)


# Config drift is invisible by nature: a service recreated without an OTLP
# endpoint keeps serving traffic, keeps logging, and simply stops tracing —
# which nobody notices until an incident needs the traces that were never
# recorded. So the process REPORTS its own tracing state and the obs stack
# alerts on it. setup_tracing owns the value; 0 is the honest default for
# any process that imports this module and never arms.
TRACING_ARMED = Gauge(
    "tracing_armed",
    "1 when this process exports spans to a collector, 0 when tracing is off.",
    registry=REGISTRY,
)


def observe_request(method: str, status: int, duration_s: float) -> None:
    """Record one completed HTTP request. Called by the middleware, once —
    inside the request's span, so the observation carries an EXEMPLAR: a
    sample trace_id pinned to the histogram bucket. Grafana renders these
    as dots you click through to Jaeger — the metrics→traces bridge."""
    exemplar = None
    span = _trace.get_current_span()
    ctx = span.get_span_context()
    if span.is_recording() and ctx.is_valid:
        exemplar = {"trace_id": format(ctx.trace_id, "032x")}
    HTTP_DURATION.labels(method=method, status=str(status)).observe(duration_s, exemplar=exemplar)


def render_metrics() -> tuple[bytes, str]:
    """The /metrics response body + its content type. OPENMETRICS format
    (not the classic exposition): it is what carries exemplars, and
    Prometheus parses it natively."""
    body = cast(bytes, _openmetrics.generate_latest(REGISTRY))  # pyright: ignore[reportUnknownMemberType]
    return body, cast(str, _openmetrics.CONTENT_TYPE_LATEST)


def serve_metrics(port: int) -> int:
    """Expose the registry over plain HTTP — for processes that are not web
    apps (the Temporal worker has no FastAPI to mount /metrics on). Returns
    the BOUND port so callers passing 0 (tests) learn what they got."""
    server, _thread = start_http_server(port, registry=REGISTRY)
    return int(server.server_port)
