"""Wire vocabulary for the refund-notification workflow (ADR-0040).

Shared by the consumer (which starts workflows BY STRING NAME so it never
imports workflow code) and the worker (which registers them). Keeping the
names here is what lets those two processes stay import-independent — the
same split order makes between its API and its worker.
"""

from dataclasses import dataclass
from enum import StrEnum

WORKFLOW_REFUND_NOTIFICATION = "RefundNotificationWorkflow"
TASK_QUEUE_DEFAULT = "notification-tq"


class ActivityName(StrEnum):
    FETCH_RECIPIENTS = "notification.fetch_recipients"
    WRITE_NOTIFICATION = "notification.write_notification"


@dataclass
class RefundNotice:
    """Everything the workflow needs, taken from the payment event itself.

    `event_id` rides along because the notification's id is derived from it
    — that derivation is what makes a Kafka redelivery (or a workflow retry)
    collide on the PK instead of minting a second bell entry."""

    event_id: str
    order_id: str
    amount_cents: int
    currency: str
    occurred_at: str  # ISO-8601; the row's created_at, replay-stable
