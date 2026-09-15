"""The two activities RefundNotificationWorkflow runs (ADR-0040).

Both are ordinary async functions over the same session factory the
consumer uses — the worker shares notification_db with the API process,
like order's worker shares order_db.
"""

from datetime import datetime
from typing import Any

import httpx
from smartfood_auth import internal_headers
from smartfood_kafka import EventType
from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from . import push
from .adapters.repo import NotificationRepo, notification_id
from .domain.mapping import payment_drafts
from .values import ActivityName, RefundNotice

log = get_logger("notification.activities")


class OrderUnavailable(Exception):
    """Transport trouble or a 5xx — Temporal retries. A 404 does NOT raise
    this: an order that does not exist will never exist, so it is a
    non-retryable application error instead of an hour of polling."""


class NotificationActivities:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        *,
        order_base_url: str,
    ):
        self._sessions = sessions
        self._http = http
        self._order = order_base_url.rstrip("/")

    def all(self) -> list[Any]:
        return [self.fetch_recipients, self.write_notification]

    @activity.defn(name=ActivityName.FETCH_RECIPIENTS)
    async def fetch_recipients(self, order_id: str) -> str:
        """Ask order who the customer is. The payments topic cannot tell us
        (events are keyed by order), and this is the call Temporal exists
        here to retry."""
        try:
            resp = await self._http.get(
                f"{self._order}/v1/internal/orders/{order_id}/recipients",
                headers=internal_headers("notification-worker"),
            )
        except httpx.HTTPError as exc:
            raise OrderUnavailable(str(exc)) from None
        if resp.status_code == 404:
            raise LookupError(f"no such order: {order_id}")
        if resp.status_code >= 500:
            raise OrderUnavailable(f"order returned {resp.status_code}")
        resp.raise_for_status()
        return str(resp.json()["user_id"])

    @activity.defn(name=ActivityName.WRITE_NOTIFICATION)
    async def write_notification(self, notice: RefundNotice, user_id: str) -> None:
        """The bell entry. Copy comes from `payment_drafts`, the same place
        every other notification's wording lives — the workflow moved WHERE
        the recipient is resolved, not who writes the sentences.

        Conflict-ignored on the deterministic id, so a workflow retry after
        a partial failure rewrites nothing."""
        drafts = payment_drafts(
            EventType.REFUND_PROCESSED,
            {"amount_cents": notice.amount_cents, "currency": notice.currency},
            user_id=user_id,
        )
        async with self._sessions() as session:
            repo = NotificationRepo(session)
            for draft in drafts:
                await repo.insert_notification(
                    id=notification_id(notice.event_id, draft.recipient_type, draft.recipient_id),
                    recipient_type=draft.recipient_type,
                    recipient_id=draft.recipient_id,
                    order_id=notice.order_id,
                    title=draft.title,
                    body=draft.body,
                    created_at=datetime.fromisoformat(notice.occurred_at),
                )
            await session.commit()
        # Post-commit, exactly as the consumer does it (S9). Without this
        # the row lands and the bell never learns: the FE DISABLES its 15s
        # poll while the stream is up, so a customer watching the bell would
        # see nothing until the stream dropped. Fails open — a lost hint
        # costs one poll interval, never the notification.
        for draft in drafts:
            await push.publish_hint(draft.recipient_type, draft.recipient_id)
        log.info("refund notification written", order_id=notice.order_id)
