"""Receipt enqueue — the post-commit nudge seam (S10).

The bell-hint pattern, fourth verse, with one twist. Same rules: fire only
after the claim-check row COMMITS (never describe a write that rolled
back), and never let a broker problem break the consumer loop that feeds
the inbox (fail open, count it, move on). The twist is who repairs the
loss: the bell leans on the FE's poll floor, receipts lean on the beat
sweeper — either way the nudge is an optimization and the DB row is the
intent record, which is exactly why no broker transaction is needed here.

kombu's publish is BLOCKING (it may retry a dead broker for a second or
two), so the enqueue runs in a thread — the Kafka consumer's event loop
keeps breathing while AMQP sulks.
"""

import asyncio
from typing import Protocol

from prometheus_client import Counter
from smartfood_otel import REGISTRY, get_logger

log = get_logger("notification.receipt_queue")

RECEIPT_ENQUEUE_FAILURES = Counter(
    "receipt_enqueue_failures_total",
    "Post-commit receipt enqueues that failed (the sweeper repairs these).",
    registry=REGISTRY,
)


class ReceiptQueue(Protocol):
    # Positional-only so anything shaped like `f(order_id)` satisfies it —
    # including a bare list.append in tests.
    def __call__(self, order_id: str, /) -> None: ...


_queue: ReceiptQueue | None = None


def set_queue(queue: ReceiptQueue | None) -> None:
    global _queue
    _queue = queue


def reset_queue() -> None:
    set_queue(None)


async def enqueue_receipt(order_id: str) -> None:
    """Nudge the chain; never let the nudge break the consume it follows."""
    if _queue is None:
        return
    try:
        await asyncio.to_thread(_queue, order_id)
    except Exception as exc:
        RECEIPT_ENQUEUE_FAILURES.inc()
        log.warning(
            "receipt enqueue dropped — the sweeper will find it",
            order_id=order_id,
            error=str(exc),
        )
