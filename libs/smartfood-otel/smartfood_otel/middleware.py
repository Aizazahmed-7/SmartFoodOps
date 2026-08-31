"""Request-context ASGI middleware.

Pure ASGI (no framework dependency): wraps any FastAPI/Starlette app. Per
request it (1) opens the hop's SERVER span, parented on the caller's
traceparent (OpenTelemetry SDK — the span IS this hop's identity),
(2) takes or mints X-Request-ID, (3) binds request_id + trace_id into
structlog's contextvars so every log line of this request carries them,
(4) echoes X-Request-ID on the response, and (5) emits one
"request completed" line plus the HTTP histogram observation — the
guaranteed per-hop match when grepping a trace_id across services.

Ops paths (/metrics, /healthz, /readyz) get no span and no histogram
entry: a scrape must not trace or measure itself, and heartbeats would
drown real traffic.
"""

import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import structlog
from opentelemetry.trace import INVALID_SPAN, Span, SpanKind, Status, StatusCode

from .logging import get_logger
from .metrics import observe_request
from .propagation import (
    current_traceparent,
    extract_context,
    extract_traceparent,
    make_traceparent,
    trace_id_of,
)
from .tracing import get_tracer

REQUEST_ID_HEADER = "x-request-id"

# Docker healthchecks and Prometheus scrapes poll these every few
# seconds — logging them would bury real traffic under heartbeat noise
# (8 services x 15s scrapes ≈ 46k /metrics lines a day, all identical).
_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}
# Ops endpoints excluded from the HTTP histogram AND from tracing: a
# metrics scrape must not measure (or trace) itself.
_METRICS_SKIP = {"/healthz", "/readyz", "/metrics"}

log = get_logger("smartfood-otel.access")

# Pure-ASGI protocol types (framework-free by design — see module docstring).
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, stream_prefixes: tuple[str, ...] = ()):
        """`stream_prefixes` names this service's held-connection endpoints
        (SSE lanes). They keep their completion LOG line (one line per
        stream close — priceless in debugging) but skip the latency
        histogram AND the span: a stream's duration is its lifetime
        POLICY, not a latency — recording it pinned notification's p95
        at the top bucket for a full day (the live find). Prefix match
        because tracking streams carry the order id in the path."""
        self.app = app
        self._stream_prefixes = stream_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        path = scope.get("path", "")
        method = scope.get("method", "")
        # One decision, applied to histogram and span alike ("" prefixes
        # never match: str.startswith(()) is False).
        measured = path not in _METRICS_SKIP and not path.startswith(self._stream_prefixes)

        # This hop's SERVER span, parented on the caller's context. With
        # tracing unconfigured the no-op tracer makes it free.
        span_cm: AbstractContextManager[Span] = (
            get_tracer().start_as_current_span(
                f"{method} {path}", context=extract_context(headers), kind=SpanKind.SERVER
            )
            if measured
            else nullcontext(INVALID_SPAN)
        )
        with span_cm as span:
            # The outgoing identity, in order of truth: the ACTIVE span
            # (tracing on → proper child ids), else the caller's header
            # passed through (tracing off → cross-service log continuity),
            # else a fresh root.
            traceparent = (
                current_traceparent() or extract_traceparent(headers) or make_traceparent()
            )

            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(
                request_id=request_id,
                trace_id=trace_id_of(traceparent),
            )
            scope.setdefault("state", {})
            scope["state"]["traceparent"] = traceparent

            status: dict[str, int | None] = {"code": None}

            async def send_with_request_id(message: Message) -> None:
                if message["type"] == "http.response.start":
                    status["code"] = message["status"]
                    message.setdefault("headers", []).append(
                        (REQUEST_ID_HEADER.encode(), request_id.encode())
                    )
                await send(message)

            started = time.perf_counter()
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                # Response never started — the outer error boundary will 500.
                status["code"] = 500
                raise
            finally:
                duration_s = time.perf_counter() - started
                if path not in _QUIET_PATHS:
                    log.info(
                        "request completed",
                        method=method,
                        path=path,
                        status=status["code"],
                        duration_ms=round(duration_s * 1000, 1),
                    )
                if measured and status["code"] is not None:
                    observe_request(method, status["code"], duration_s)
                    if span.is_recording():
                        span.set_attribute("http.method", method)
                        span.set_attribute("http.route", path)
                        span.set_attribute("http.status_code", status["code"])
                        if status["code"] >= 500:
                            span.set_status(Status(StatusCode.ERROR))
