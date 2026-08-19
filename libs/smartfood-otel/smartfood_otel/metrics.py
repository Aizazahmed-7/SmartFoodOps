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

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Histogram, generate_latest

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


def observe_request(method: str, status: int, duration_s: float) -> None:
    """Record one completed HTTP request. Called by the middleware, once."""
    HTTP_DURATION.labels(method=method, status=str(status)).observe(duration_s)


def render_metrics() -> tuple[bytes, str]:
    """The /metrics response body + its content type — the text exposition
    format Prometheus scrapes."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
