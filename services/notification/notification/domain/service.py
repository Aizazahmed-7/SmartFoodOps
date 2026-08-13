"""Inbox reads and read-marking — the API layer's only dependency.

Writes arrive exclusively through the consumer handler; this service owns
the read side: one keyset page + unread count per poll, and idempotent
read-marking with ownership in the WHERE (not-yours and unknown are the
same 404 to the caller — the inbox never confirms other people's mail)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import NotificationRepo, decode_cursor, encode_cursor


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)  # sqlite drops tz


class NotificationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def inbox(
        self, recipient_type: str, recipient_id: str, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """One poll's worth: a page plus the unread badge count. Raises
        ValueError on a malformed cursor (route maps to 422)."""
        before = decode_cursor(cursor) if cursor is not None else None
        async with self._sessions() as session:
            repo = NotificationRepo(session)
            rows = await repo.inbox_page(recipient_type, recipient_id, limit=limit, before=before)
            unread = await repo.unread_count(recipient_type, recipient_id)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = encode_cursor(_aware(page[-1].created_at), page[-1].id) if has_more else None
        return {
            "items": [
                {
                    "id": row.id,
                    "order_id": row.order_id,
                    "kind": row.kind,
                    "title": row.title,
                    "body": row.body,
                    "created_at": _aware(row.created_at).isoformat(),
                    "read_at": _aware(row.read_at).isoformat() if row.read_at else None,
                }
                for row in page
            ],
            "next_cursor": next_cursor,
            "unread": unread,
        }

    async def mark_read(
        self, recipient_type: str, recipient_id: str, notification_id: str
    ) -> str | None:
        """None = not yours or unknown (the route's 404). Idempotent: the
        first read's timestamp survives replays of the tap."""
        async with self._sessions() as session:
            read_at = await NotificationRepo(session).mark_read(
                recipient_type, recipient_id, notification_id, _now()
            )
            await session.commit()
        return _aware(read_at).isoformat() if read_at else None

    async def mark_all_read(self, recipient_type: str, recipient_id: str) -> int:
        async with self._sessions() as session:
            marked = await NotificationRepo(session).mark_all_read(
                recipient_type, recipient_id, _now()
            )
            await session.commit()
        return marked
