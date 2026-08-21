"""W3C trace-context helpers.

A traceparent looks like:  00-<32 hex trace id>-<16 hex span id>-01
The trace id is the thread that ties one order's journey together across
services, Kafka headers, and outbox rows. Week 3 replaces generation with
the real OTel SDK; the header format is a standard, so nothing else changes.
"""

import re
import secrets
from collections.abc import Generator, Mapping
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")

HEADER = "traceparent"

# W3C TraceContext propagator — the ONE codec for the header, both ways.
_PROPAGATOR = TraceContextTextMapPropagator()


def current_traceparent() -> str | None:
    """The ACTIVE OTel span's traceparent — None outside any span (pollers,
    consumers between messages, startup). Same contract as the old
    contextvar version; the storage moved into OTel's context."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get(HEADER)


def extract_context(headers: Mapping[str, str]) -> Context:
    """Build the OTel context a child span should start under, from carrier
    headers (HTTP or Kafka). Missing/garbage header → empty context → the
    child becomes a root span, never an error."""
    return _PROPAGATOR.extract(carrier=dict(headers))


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


def span_id_of(traceparent: str) -> str | None:
    m = _TRACEPARENT_RE.match(traceparent)
    return m.group(2) if m else None


@contextmanager
def use_traceparent(traceparent: str) -> Generator[None]:
    """Run a block AS IF inside a request that carried this traceparent:
    the extracted context is attached, so current_traceparent() and header
    injection see it. The seam tests and out-of-request tools use where a
    real server span does the attaching in production."""
    token = otel_context.attach(extract_context({HEADER: traceparent}))
    try:
        yield
    finally:
        otel_context.detach(token)
