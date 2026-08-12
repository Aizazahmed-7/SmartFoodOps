"""SagaPort stand-in until S5 wires Temporal.

Orders placed while this is live sit honestly at PLACED — visible in the
UI, replayable by the W3 sweeper design (the OrderPlaced outbox event is
the durable to-do). The log line makes the gap observable, not silent."""

from smartfood_otel import get_logger

log = get_logger("order.saga")


class SagaNotYetWired:
    async def start(self, order_id: str) -> None:
        log.info("saga start deferred — Temporal lands in S5", order_id=order_id)
