"""Structured JSON logging for every SmartFoodOps process.

Week-1 scope: structlog only. Week 3 adds the OpenTelemetry SDK; the public
surface here (setup_logging, bind_order, get_logger) is what services code
against, so that swap changes no service code (docs §14).
"""

import logging

import structlog


def setup_logging(service: str, *, level: int = logging.INFO) -> None:
    """Configure process-wide JSON logs: one line per event, UTC timestamps.

    Every line carries `service`, plus whatever the request middleware and
    bind_order() have put into context (request_id, trace_id, order_id) —
    that is what makes `grep order_id=... across services` work.
    """

    def add_service(_logger, _method, event_dict: dict) -> dict:
        event_dict["service"] = service
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_service,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_order(order_id: str) -> None:
    """Attach an order id to every subsequent log line on this task/request."""
    structlog.contextvars.bind_contextvars(order_id=order_id)
