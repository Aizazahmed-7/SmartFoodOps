"""W3C trace-context helpers.

A traceparent looks like:  00-<32 hex trace id>-<16 hex span id>-01
The trace id is the thread that ties one order's journey together across
services, Kafka headers, and outbox rows. Week 3 replaces generation with
the real OTel SDK; the header format is a standard, so nothing else changes.
"""

import re
import secrets
from collections.abc import Mapping
from contextvars import ContextVar

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")

HEADER = "traceparent"

# Request-scoped, set by RequestContextMiddleware. A plain ContextVar (not a
# structlog contextvar) on purpose: log lines already carry trace_id; the
# full traceparent is transport context for outbox rows / Kafka headers,
# not log noise.
_current_traceparent: ContextVar[str | None] = ContextVar("traceparent", default=None)


def current_traceparent() -> str | None:
    """The active request's traceparent — None outside a request (pollers,
    consumers, startup)."""
    return _current_traceparent.get()


def set_current_traceparent(traceparent: str) -> None:
    _current_traceparent.set(traceparent)


def make_traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def extract_traceparent(headers: Mapping[str, str]) -> str | None:
    """Return the traceparent header if present and well-formed, else None."""
    value = headers.get(HEADER) or headers.get(HEADER.title())
    if value and _TRACEPARENT_RE.match(value):
        return value
    return None


def trace_id_of(traceparent: str) -> str | None:
    m = _TRACEPARENT_RE.match(traceparent)
    return m.group(1) if m else None
