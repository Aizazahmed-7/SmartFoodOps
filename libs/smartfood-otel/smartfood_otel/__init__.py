"""smartfood-otel — logging, metrics, and OpenTelemetry tracing.

setup_logging() + setup_tracing() at service startup,
RequestContextMiddleware on the app, bind_order() when an order enters the
picture. The tracing internals are the real OpenTelemetry SDK (the week-1
docstring promised this swap; it landed without changing this surface).
"""

from .logging import bind_order, get_logger, setup_logging
from .metrics import REGISTRY, observe_request, render_metrics, serve_metrics
from .middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from .propagation import (
    current_traceparent,
    extract_context,
    extract_traceparent,
    make_traceparent,
    span_id_of,
    trace_id_of,
    use_traceparent,
)
from .tracing import get_tracer, reset_tracing, setup_tracing

__all__ = [
    "REGISTRY",
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "bind_order",
    "current_traceparent",
    "extract_context",
    "extract_traceparent",
    "get_logger",
    "get_tracer",
    "make_traceparent",
    "observe_request",
    "render_metrics",
    "reset_tracing",
    "serve_metrics",
    "setup_logging",
    "setup_tracing",
    "span_id_of",
    "trace_id_of",
    "use_traceparent",
]
