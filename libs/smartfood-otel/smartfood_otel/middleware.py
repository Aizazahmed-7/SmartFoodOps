"""Request-context ASGI middleware.

Pure ASGI (no framework dependency): wraps any FastAPI/Starlette app. Per
request it (1) clears stale context, (2) takes or mints X-Request-ID and
traceparent, (3) binds request_id + trace_id into structlog's contextvars so
every log line of this request carries them, (4) echoes X-Request-ID on the
response so clients can quote it in bug reports.
"""

import uuid

import structlog

from .propagation import extract_traceparent, make_traceparent, trace_id_of

REQUEST_ID_HEADER = "x-request-id"


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
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
        scope.setdefault("state", {})
        scope["state"]["traceparent"] = traceparent

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (REQUEST_ID_HEADER.encode(), request_id.encode())
                )
            await send(message)

        await self.app(scope, receive, send_with_request_id)
