"""The error envelope from docs/api-standards.md §2.

Every non-2xx response, from every service, is:
    {"error": {"code": "<catalog code>", "message": "...", "request_id": "..."}}
with optional "details" on 422s. Services raise ApiError (or let validation
fail); install_error_handlers() renders the envelope. Codes come from the
catalog table — adding one is a PR to that table, never an inline string.
"""

# pyright: reportUnusedFunction=false
# (decorator-registered handlers; the global override doesn't reach
# strict-tier files)

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from smartfood_otel import get_logger
from starlette.exceptions import HTTPException as StarletteHTTPException

log = get_logger("smartfood-api")


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        details: list[dict[str, str]] | None = None,
    ):
        self.code = code
        self.message = message
        self.status = status
        self.headers = headers or {}
        self.details = details
        super().__init__(f"{code}: {message}")


def _request_id() -> str:
    return structlog.contextvars.get_contextvars().get("request_id", "")


def envelope(
    code: str, message: str, details: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message, "request_id": _request_id()}
    }
    if details:
        body["error"]["details"] = details
    return body


# Fallback codes for bare HTTPExceptions (services should prefer ApiError).
_STATUS_CODES = {
    401: "AUTH_INVALID_CREDENTIALS",
    403: "FORBIDDEN_ROLE",
    404: "NOT_FOUND",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMITED",
    503: "DEPENDENCY_UNAVAILABLE",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=envelope(exc.code, exc.message, exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "issue": str(err.get("msg", "invalid")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=envelope("VALIDATION_FAILED", "request validation failed", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "INTERNAL_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(code, str(exc.detail)),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals; the log line carries the real error + request_id.
        log.error("unhandled exception", error=repr(exc))
        return JSONResponse(
            status_code=500,
            content=envelope("INTERNAL_ERROR", "internal error"),
        )
