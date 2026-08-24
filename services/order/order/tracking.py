"""Live order-status fan-out — the process-global publisher seam.

Every status write in this service funnels through three commit points
(domain.transitions.transition, begin_cancel_from, and create_order), so
those three call publish_status() AFTER their commit and the entire
lifecycle is covered without threading a publisher through every
signature. Module state mirrors the otel/metrics idiom: armed once at
process startup (API and worker BOTH publish — the worker owns most
transitions), reset in tests.

Publishing is a HINT, not a record. The stream tells the customer "look
again"; the database remains the only truth the FE renders. That one
decision buys the whole failure story: a publish lost to a Redis blip
costs a few seconds of staleness (the poll fallback still exists), a
publish raced by a rollback costs one harmless refetch — so the bus may
fail OPEN and stay entirely off the money path. No-raise, logged, same
contract as every other degrade in this codebase.
"""

from typing import Protocol

from smartfood_otel import get_logger

log = get_logger("order.tracking")


class StatusPublisher(Protocol):
    async def publish(self, channel: str, data: str) -> None: ...


def track_channel(order_id: str) -> str:
    """The one place the tracking channel name is spelled."""
    return f"sfo:track:{order_id}"


_publisher: StatusPublisher | None = None


def set_publisher(publisher: StatusPublisher | None) -> None:
    global _publisher
    _publisher = publisher


def reset_publisher() -> None:
    set_publisher(None)


async def publish_status(order_id: str, status: str) -> None:
    """Fire the hint; never let the hint break the transition it describes."""
    if _publisher is None:
        return
    try:
        await _publisher.publish(track_channel(order_id), status)
    except Exception as exc:
        log.warning(
            "status publish failed — stream stays stale until the FE polls",
            order_id=order_id,
            error=str(exc),
        )
