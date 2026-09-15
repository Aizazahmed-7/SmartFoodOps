"""The inbox handler — notification's only write path.

TWO EventConsumers feed this one handler — one per topic, with separate
groups (the loop and its retry/DLQ policy are smartfood_kafka.EventConsumer,
ADR-0021):

    GROUP_ORDERS    ← c1.orders.events
    GROUP_PAYMENTS  ← c1.payments.events

Separate loops on purpose — now for failure isolation rather than for a
projection race (ADR-0040 replaced that with a workflow): separate consumer
groups mean separate offsets, so a poison payment event parking on the DLQ
cannot stall order notifications, or the reverse.

Payment events carry no user_id (they are keyed by order). A refund starts
RefundNotificationWorkflow, which asks order who the customer is and lets
Temporal own that retry — so an order outage delays the bell entry instead
of DLQ-ing it. The handler's own job is just to start the workflow, which
is why it returns immediately rather than awaiting the result.

Dedupe mode: NATURAL_KEY — notification ids are deterministic per
(event, recipient), so at-least-once redelivery collides on the PK and is
conflict-ignored. No processed_events table.
"""

import json
from datetime import UTC, datetime
from typing import Any

from smartfood_kafka import EventType
from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import push, receipt_queue
from .adapters.repo import NotificationRepo, notification_id
from .adapters.saga_client import RefundNotifierPort
from .domain.mapping import NOTIFYING_PAYMENT_EVENTS, Draft, order_drafts
from .values import RefundNotice

log = get_logger("notification.consumers")

GROUP_ORDERS = "notification.inbox.orders"
GROUP_PAYMENTS = "notification.inbox.payments"


def _aware(value: Any) -> datetime:
    # Avro decode yields aware datetimes; the JSON test serde yields ISO
    # strings. Either way the row gets a tz-aware timestamp.
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


class InboxHandler:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], refunds: RefundNotifierPort):
        self._sessions = sessions
        self._refunds = refunds

    async def handle(self, event: dict[str, Any]) -> None:
        event_type = str(event["event_type"])
        if event.get("aggregate_type") == "payment" and event_type not in NOTIFYING_PAYMENT_EVENTS:
            return  # silent payment fact — must not even touch the projection
        payload = json.loads(event["payload"])
        occurred_at = _aware(event["occurred_at"])
        receipt_minted = False
        async with self._sessions() as session:
            repo = NotificationRepo(session)
            drafts: list[Draft]
            if event.get("aggregate_type") == "order":
                order_id = str(payload["order_id"])
                # EVERY order event refreshes the projection, so whichever
                # event arrives first arms the payment join.
                drafts = order_drafts(event_type, payload)
                if event_type == EventType.ORDER_SETTLED:
                    # The bell stays silent on settlement (mapping.py) — but
                    # a receipt is owed the moment the money is captured, so
                    # the claim check joins THIS transaction: row and inbox
                    # commit together or not at all. Replays collide on the
                    # PK (minted=False) and owe nothing.
                    receipt_minted = await repo.insert_receipt(
                        order_id=order_id,
                        user_id=str(payload["user_id"]),
                        restaurant_name=str(payload["restaurant_name"]),
                        snapshot={"items": payload["items"], "totals": payload["totals"]},
                        settled_at=occurred_at,
                        created_at=datetime.now(UTC),
                    )
            elif event.get("aggregate_type") == "payment":
                # Handed to Temporal, not minted here: the recipient is not
                # in this payload and the lookup can outlive the poll loop.
                await self._refunds.notify_refund(
                    RefundNotice(
                        event_id=str(event["event_id"]),
                        order_id=str(event["aggregate_id"]),
                        amount_cents=int(payload["amount_cents"]),
                        currency=str(payload["currency"]),
                        occurred_at=occurred_at.isoformat(),
                    )
                )
                return
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
        # Post-commit, one hint per DISTINCT recipient (S9): the hint must
        # never describe a row that rolled back, and its failure must never
        # undo one that landed — the order-tracking transition rule, applied
        # to the bell. Duplicate-safe by construction: the FE's reaction is
        # a refetch, and refetching twice shows the same truth.
        for recipient_type, recipient_id in {(d.recipient_type, d.recipient_id) for d in drafts}:
            await push.publish_hint(recipient_type, recipient_id)
        # Same post-commit rule for the receipt nudge (S10): fresh row →
        # one best-effort enqueue; failure is COUNTED and left to the beat
        # sweeper, never allowed to fail the batch that carried the event.
        if receipt_minted:
            await receipt_queue.enqueue_receipt(order_id)
