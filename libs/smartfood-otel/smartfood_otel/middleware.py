"""Request-context ASGI middleware.

Pure ASGI (no framework dependency): wraps any FastAPI/Starlette app. Per
request it (1) clears stale context, (2) takes or mints X-Request-ID and
traceparent, (3) binds request_id + trace_id into structlog's contextvars so
every log line of this request carries them, (4) echoes X-Request-ID on the
response so clients can quote it in bug reports, and (5) emits one
"request completed" line — the guaranteed per-hop match when grepping a
trace_id or request_id across services.
"""

import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog

from .logging import get_logger
from .metrics import observe_request
from .propagation import (
    extract_traceparent,
    make_traceparent,
    set_current_traceparent,
    trace_id_of,
)

REQUEST_ID_HEADER = "x-request-id"

# Docker healthchecks poll these every few seconds — logging them would
# bury real traffic under heartbeat noise.
_QUIET_PATHS = {"/healthz", "/readyz"}
# Ops endpoints excluded from the HTTP histogram: a metrics scrape must
# not measure itself, and the health probes are heartbeat noise.
_METRICS_SKIP = {"/healthz", "/readyz", "/metrics"}

log = get_logger("smartfood-otel.access")

# Pure-ASGI protocol types (framework-free by design — see module docstring).
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        traceparent = extract_traceparent(headers) or make_traceparent()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            trace_id=trace_id_of(traceparent),
        )
        set_current_traceparent(traceparent)
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
            path = scope.get("path", "")
            duration_s = time.perf_counter() - started
            if path not in _QUIET_PATHS:
                log.info(
                    "request completed",
                    method=scope.get("method", ""),
                    path=path,
                    status=status["code"],
                    duration_ms=round(duration_s * 1000, 1),
                )
            # One histogram observation per request, from the same numbers —
            # status is None only if the response never started (a crash the
            # error boundary turns into 500, already stamped above).
            if path not in _METRICS_SKIP and status["code"] is not None:
                observe_request(scope.get("method", ""), status["code"], duration_s)
