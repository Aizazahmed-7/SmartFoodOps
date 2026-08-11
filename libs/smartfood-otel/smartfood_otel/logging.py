"""Structured JSON logging for every SmartFoodOps process.

Week-1 scope: structlog only. Week 3 adds the OpenTelemetry SDK; the public
surface here (setup_logging, bind_order, get_logger) is what services code
against, so that swap changes no service code (docs §14).
"""

import logging
import os

import structlog
from structlog.typing import Processor


def setup_logging(service: str, *, level: int | None = None) -> None:
    """Configure process-wide JSON logs: one line per event, UTC timestamps.

    Every line carries `service`, plus whatever the request middleware and
    bind_order() have put into context (request_id, trace_id, order_id) —
    that is what makes `grep order_id=... across services` work.

    Level: explicit arg wins; else LOG_LEVEL env (e.g. DEBUG turns on the
    per-op cache hit/miss lines); unknown names fall back to INFO.
    """
    if level is None:
        name = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = logging.getLevelNamesMapping().get(name, logging.INFO)

    def add_service(
        _logger: structlog.typing.WrappedLogger,
        _method: str,
        event_dict: structlog.typing.EventDict,
    ) -> structlog.typing.EventDict:
        event_dict["service"] = service
        return event_dict

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        add_service,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_order(order_id: str) -> None:
    """Attach an order id to every subsequent log line on this task/request."""
    structlog.contextvars.bind_contextvars(order_id=order_id)
