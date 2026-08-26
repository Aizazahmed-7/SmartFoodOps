"""SQL for the inbox — conflict-safe writes, keyset reads.

Write-side dedupe is structural: notification ids are deterministic per
(event, recipient), so an at-least-once redelivery re-inserts the same PK
and ON CONFLICT DO NOTHING absorbs it. Same idiom as inventory's
provisioning (its repo docstring explains why this beats try/except
IntegrityError across the PG/sqlite dialect split).
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import notifications, order_recipients, receipts

# Fixed namespace for notification ids. NEVER change it: ids are the dedupe
# key, and a new namespace would re-deliver every replayed event's inbox row.
_NAMESPACE = uuid.UUID("4b8c1a6e-2d5f-4f9b-9c3a-7e1d8a0b5c42")


def notification_id(event_id: str, recipient_type: str, recipient_id: str) -> str:
    """Same fact for the same recipient, same id, always — one event fans
    out to two recipients as two distinct, individually-deduped rows."""
    return "ntf_" + uuid.uuid5(_NAMESPACE, f"{event_id}:{recipient_type}:{recipient_id}").hex


def encode_cursor(created_at: datetime, notification_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), notification_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Raises ValueError on ANY malformed cursor — the route maps to 422.
    Normalized: valid base64 of the wrong JSON shape raises TypeError
    variants internally, and a cursor bug must never surface as a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        created_at_iso, row_id = json.loads(raw)
        return datetime.fromisoformat(created_at_iso), str(row_id)
    except (TypeError, ValueError) as exc:  # binascii/JSON errors are ValueErrors
        raise ValueError("malformed cursor") from exc


def insert_ignoring_conflict(
    table: sa.Table, values: dict[str, Any], conflict_cols: list[str], dialect: str
) -> Any:
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    return insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict_cols)


class NotificationRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    async def upsert_recipients(self, order_id: str, user_id: str, restaurant_id: str) -> None:
        """First order event wins; the pair never changes for an order."""
        await self._s.execute(
            insert_ignoring_conflict(
                order_recipients,
                {"order_id": order_id, "user_id": user_id, "restaurant_id": restaurant_id},
                ["order_id"],
                self._dialect,
            )
        )

    async def get_recipients(self, order_id: str) -> Row[Any] | None:
        result = await self._s.execute(
            sa.select(order_recipients).where(order_recipients.c.order_id == order_id)
        )
        return result.one_or_none()

    async def insert_notification(
        self,
        *,
        id: str,
        recipient_type: str,
        recipient_id: str,
        order_id: str,
        kind: str,
        title: str,
        body: str,
        created_at: datetime,
    ) -> None:
        await self._s.execute(
            insert_ignoring_conflict(
                notifications,
                {
                    "id": id,
                    "recipient_type": recipient_type,
                    "recipient_id": recipient_id,
                    "order_id": order_id,
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "created_at": created_at,
                },
                ["id"],
                self._dialect,
            )
        )

    async def insert_receipt(
        self,
        *,
        order_id: str,
        user_id: str,
        restaurant_name: str,
        items: list[dict[str, Any]],
        totals: dict[str, Any],
        settled_at: datetime,
        created_at: datetime,
    ) -> bool:
        """File the claim check (S10). True = fresh insert (the caller owes
        an enqueue); False = a replay collided on the PK and there is
        nothing to do — the first delivery of this event already enqueued,
        and the sweeper covers it even if that enqueue was lost."""
        result = await self._s.execute(
            insert_ignoring_conflict(
                receipts,
                {
                    "order_id": order_id,
                    "user_id": user_id,
                    "restaurant_name": restaurant_name,
                    "items": items,
                    "totals": totals,
                    "settled_at": settled_at,
                    "created_at": created_at,
                },
                ["order_id"],
                self._dialect,
            )
        )
        return cast(CursorResult[Any], result).rowcount > 0

    async def inbox_page(
        self,
        recipient_type: str,
        recipient_id: str,
        *,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[Row[Any]]:
        """Keyset page down ix_notifications_inbox: newest first, `before`
        is the exclusive continuation point. limit+1 rows = has_more probe."""
        query = (
            sa.select(notifications)
            .where(
                (notifications.c.recipient_type == recipient_type)
                & (notifications.c.recipient_id == recipient_id)
            )
            .order_by(notifications.c.created_at.desc(), notifications.c.id.desc())
            .limit(limit + 1)
        )
        if before is not None:
            # Row-value comparison: id is the tiebreaker for notifications
            # minted from the same instant.
            query = query.where(sa.tuple_(notifications.c.created_at, notifications.c.id) < before)
        result = await self._s.execute(query)
        return list(result.all())

    async def unread_count(self, recipient_type: str, recipient_id: str) -> int:
        result = await self._s.execute(
            sa.select(sa.func.count())
            .select_from(notifications)
            .where(
                (notifications.c.recipient_type == recipient_type)
                & (notifications.c.recipient_id == recipient_id)
                & (notifications.c.read_at.is_(None))
            )
        )
        return result.scalar_one()

    async def mark_read(
        self, recipient_type: str, recipient_id: str, notification_id: str, now: datetime
    ) -> datetime | None:
        """Ownership in the WHERE: not-yours and unknown are the same None.
        Idempotent — a re-read keeps the ORIGINAL read_at."""
        await self._s.execute(
            notifications.update()
            .where(
                (notifications.c.id == notification_id)
                & (notifications.c.recipient_type == recipient_type)
                & (notifications.c.recipient_id == recipient_id)
                & (notifications.c.read_at.is_(None))
            )
            .values(read_at=now)
        )
        result = await self._s.execute(
            sa.select(notifications.c.read_at).where(
                (notifications.c.id == notification_id)
                & (notifications.c.recipient_type == recipient_type)
                & (notifications.c.recipient_id == recipient_id)
            )
        )
        row = result.one_or_none()
        return None if row is None else row.read_at

    async def mark_all_read(self, recipient_type: str, recipient_id: str, now: datetime) -> int:
        result = await self._s.execute(
            notifications.update()
            .where(
                (notifications.c.recipient_type == recipient_type)
                & (notifications.c.recipient_id == recipient_id)
                & (notifications.c.read_at.is_(None))
            )
            .values(read_at=now)
        )
        return cast(CursorResult[Any], result).rowcount
