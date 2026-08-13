"""The inbox handler — notification's only write path.

TWO EventConsumers feed this one handler — one per topic, with separate
groups (the loop and its retry/DLQ policy are smartfood_kafka.EventConsumer,
ADR-0021):

    GROUP_ORDERS    ← c1.orders.events
    GROUP_PAYMENTS  ← c1.payments.events

Separate loops on purpose. Payment events carry no user_id (they are keyed
by order), so refunds join through the order_recipients projection that
ORDER events write. Cross-topic ordering is not guaranteed — a refund can
beat its own order's events through the pipeline — and a projection miss
raises ProjectionLag so the runtime's backoff retries. That healing only
works because the orders loop keeps consuming (and arming the projection)
WHILE the payments loop backs off; one shared loop would block the very
event it is waiting for, guaranteeing the DLQ. A refund that still misses
after the backoff horizon is a true orphan: parked, visible, replayable.

Dedupe mode: NATURAL_KEY — notification ids are deterministic per
(event, recipient), so at-least-once redelivery collides on the PK and is
conflict-ignored. No processed_events table.
"""

import json
from datetime import UTC, datetime
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.repo import NotificationRepo, notification_id
from .domain.mapping import (
    NOTIFYING_PAYMENT_EVENTS,
    Draft,
    order_drafts,
    payment_drafts,
)

log = get_logger("notification.consumers")

GROUP_ORDERS = "notification.inbox.orders"
GROUP_PAYMENTS = "notification.inbox.payments"


class ProjectionLag(RuntimeError):
    """A payment event arrived before any of its order's events."""


def _aware(value: Any) -> datetime:
    # Avro decode yields aware datetimes; the JSON test serde yields ISO
    # strings. Either way the row gets a tz-aware timestamp.
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class InboxHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def handle(self, event: dict[str, Any]) -> None:
        event_type = str(event["event_type"])
        if event.get("aggregate_type") == "payment" and event_type not in NOTIFYING_PAYMENT_EVENTS:
            return  # silent payment fact — must not even touch the projection
        payload = json.loads(event["payload"])
        occurred_at = _aware(event["occurred_at"])
        async with self._sessions() as session:
            repo = NotificationRepo(session)
            drafts: list[Draft]
            if event.get("aggregate_type") == "order":
                order_id = str(payload["order_id"])
                # EVERY order event refreshes the projection, so whichever
                # event arrives first arms the payment join.
                await repo.upsert_recipients(order_id, payload["user_id"], payload["restaurant_id"])
                drafts = order_drafts(event_type, payload)
            elif event.get("aggregate_type") == "payment":
                order_id = str(event["aggregate_id"])
                recipients = await repo.get_recipients(order_id)
                if recipients is None:
                    raise ProjectionLag(f"no recipients projected yet for {order_id}")
                drafts = payment_drafts(event_type, payload, user_id=recipients.user_id)
            else:
                return  # not a topic we mint notifications from

            for draft in drafts:
                await repo.insert_notification(
                    id=notification_id(
                        str(event["event_id"]), draft.recipient_type, draft.recipient_id
                    ),
                    recipient_type=draft.recipient_type,
                    recipient_id=draft.recipient_id,
                    order_id=order_id,
                    kind=draft.kind,
                    title=draft.title,
                    body=draft.body,
                    created_at=occurred_at,
                )
            await session.commit()
            if drafts:
                log.info(
                    "notifications minted",
                    event_type=event_type,
                    order_id=order_id,
                    recipients=[draft.recipient_type for draft in drafts],
                )
