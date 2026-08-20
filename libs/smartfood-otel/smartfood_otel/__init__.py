"""smartfood-otel — logging + trace-context plumbing (week-1 scope).

setup_logging() at service startup, RequestContextMiddleware on the app,
bind_order() when an order enters the picture. Week 3 swaps the internals
for the real OpenTelemetry SDK without changing this surface.
"""

from .logging import bind_order, get_logger, setup_logging
from .metrics import REGISTRY, observe_request, render_metrics, serve_metrics
from .middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from .propagation import (
    current_traceparent,
    extract_traceparent,
    make_traceparent,
    trace_id_of,
)

__all__ = [
    "current_traceparent",
    "REGISTRY",
    "observe_request",
    "render_metrics",
    "serve_metrics",
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "bind_order",
    "extract_traceparent",
    "get_logger",
    "make_traceparent",
    "setup_logging",
    "trace_id_of",
]
