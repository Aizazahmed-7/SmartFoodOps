"""The conversation store (ADR-0042).

Two readers with different needs share these tables, and the split is what
the API shape is for: a TURN appends (a question, then chunks, then the
assembled answer), while a RECONNECT only ever reads — what was already
said, and from which `seq`.

Nothing here decides anything. The monotonic `seq`, the write-before-publish
ordering and the status transitions are the turn's business; this is where
they land.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from smartfood_kafka import EventType
from smartfood_outbox import stage_event as stage_outbox_event
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import conversations, message_chunks, messages, outbox

STREAMING = "streaming"
COMPLETE = "complete"
FAILED = "failed"


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    content: str
    status: str
    item_ids: Sequence[str] = ()


class ConversationRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    # ── the turn's writes ───────────────────────────────────────────

    async def ensure_conversation(
        self, *, conversation_id: str, user_id: str, city: str, now: datetime
    ) -> bool:
        """Create on first use, touch on every later one. **False when the
        conversation exists and is somebody else's.**

        The ownership predicate is in the UPSERT, not a read before it. A
        read-then-write would be a TOCTOU window, and more importantly it
        would be a second place the rule lives — this way the database
        cannot be persuaded to attach a turn to a conversation its owner
        does not match, by any caller, ever.

        Found by the B3 adversarial review, and it was not a small hole:
        `conversation_id` is client-supplied, so without this a customer
        could file a turn into a stranger's conversation. `repo.history()`
        would then feed that stranger's questions and answers into their
        prompt as context, and the idempotency path would hand them a live
        stream ticket for the stranger's answer.

        Insert-if-absent rather than upsert on `city`: a conversation's scope
        is set by the question that started it, and letting a later turn
        silently move it would mean a follow-up answering from a different
        city than the one the customer was reading.
        """
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        stmt = insert(conversations).values(
            id=conversation_id, user_id=user_id, city=city, created_at=now, updated_at=now
        )
        result = cast(
            CursorResult[Any],
            await self._s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[conversations.c.id],
                    set_={"updated_at": now},
                    where=conversations.c.user_id == user_id,
                )
            ),
        )
        return result.rowcount == 1

    async def start_message(
        self,
        *,
        message_id: str,
        conversation_id: str,
        role: str,
        content: str,
        status: str,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> bool:
        """Append a message. Returns False when the idempotency key has
        already been used in this conversation.

        ADR-0024's pattern rather than a separate key table: the row IS the
        record, so a retried POST loses to the unique constraint instead of
        starting a second generation — which would be a second provider bill
        and two different answers for one question.
        """
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        stmt = insert(messages).values(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            status=status,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        result = await self._s.execute(
            stmt.on_conflict_do_nothing(
                index_elements=[messages.c.conversation_id, messages.c.idempotency_key]
            )
            if idempotency_key
            else stmt
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def append_chunk(self, *, message_id: str, seq: int, content: str, now: datetime) -> None:
        """Persist one chunk. Called BEFORE the chunk is published to the bus
        (ADR-0042 §2): a chunk on the bus that is not yet here is one a
        reconnecting reader cannot be told about."""
        await self._s.execute(
            message_chunks.insert().values(
                message_id=message_id, seq=seq, content=content, created_at=now
            )
        )

    async def finish_message(
        self, *, message_id: str, content: str, status: str, item_ids: Sequence[str] = ()
    ) -> bool:
        """Settle a streaming message, ONCE. True if this call is the one
        that settled it.

        Guarded on `status = streaming`, which is what makes the interaction
        fact safe to stage beside it (ADR-0035): a replayed turn loses the
        UPDATE and therefore never reaches `stage_event`, so the outbox
        cannot grow a second fact for one answer. An unguarded UPDATE would
        make the guard "nobody calls this twice", which is a hope rather
        than an invariant — and every metric downstream is a COUNT.
        """
        result = cast(
            CursorResult[Any],
            await self._s.execute(
                sa.update(messages)
                .where(messages.c.id == message_id, messages.c.status == STREAMING)
                .values(content=content, status=status, item_ids=list(item_ids))
            ),
        )
        return result.rowcount == 1

    async def stage_interaction(
        self, *, message_id: str, payload: dict[str, Any], now: datetime
    ) -> None:
        """One interaction fact, in the caller's transaction (FR-94).

        Keyed by `message_id` and carrying ABSOLUTE values, never deltas: the
        outbox is at-least-once, so a re-drained row has to converge on the
        same fact rather than add to a counter twice.
        """
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="interaction",
            aggregate_id=message_id,
            event_type=EventType.ASSISTANT_INTERACTION,
            payload=payload,
            now=now,
        )

    # ── the reconnect's reads ───────────────────────────────────────

    async def message(self, message_id: str) -> Message | None:
        row = (
            await self._s.execute(
                sa.select(
                    messages.c.id,
                    messages.c.role,
                    messages.c.content,
                    messages.c.status,
                    messages.c.item_ids,
                ).where(messages.c.id == message_id)
            )
        ).first()
        return (
            Message(
                id=row.id,
                role=row.role,
                content=row.content,
                status=row.status,
                item_ids=list(row.item_ids or ()),
            )
            if row
            else None
        )

    async def message_for_key(self, *, conversation_id: str, key: str) -> Message | None:
        """The message a previous request with this key already created.

        The retry path: `start_message` refused the insert, and the caller
        needs the id of the turn that IS running so it can stream that one
        instead of starting a second."""
        row = (
            await self._s.execute(
                sa.select(
                    messages.c.id, messages.c.role, messages.c.content, messages.c.status
                ).where(
                    messages.c.conversation_id == conversation_id,
                    messages.c.idempotency_key == key,
                )
            )
        ).first()
        return (
            Message(id=row.id, role=row.role, content=row.content, status=row.status)
            if row
            else None
        )

    async def owner_of(self, message_id: str) -> str | None:
        """The customer a message belongs to, for the re-ticket check.

        Read through the conversation rather than stamped on the message:
        one owner per conversation is the invariant, and a second copy of it
        on every message is a second thing that can disagree.
        """
        return await self._s.scalar(
            sa.select(conversations.c.user_id)
            .select_from(messages.join(conversations))
            .where(messages.c.id == message_id)
        )

    async def chunks_after(self, *, message_id: str, seq_upto: int) -> list[tuple[int, str]]:
        """The replay buffer: everything already written past the reader's
        position, in order. `seq_upto=0` is a fresh reader and gets all of it."""
        rows = await self._s.execute(
            sa.select(message_chunks.c.seq, message_chunks.c.content)
            .where(message_chunks.c.message_id == message_id, message_chunks.c.seq > seq_upto)
            .order_by(message_chunks.c.seq)
        )
        return [(row.seq, row.content) for row in rows]

    async def history(self, *, conversation_id: str, limit: int) -> Sequence[Message]:
        """The most recent turns, oldest-first for the prompt.

        Bounded because a prompt is bounded: an unbounded history is a
        context cap violation waiting for a chatty customer (ADR-0030 §5).
        """
        rows = await self._s.execute(
            sa.select(messages.c.id, messages.c.role, messages.c.content, messages.c.status)
            .where(messages.c.conversation_id == conversation_id)
            .order_by(messages.c.created_at.desc(), messages.c.id.desc())
            .limit(limit)
        )
        found = [Message(id=r.id, role=r.role, content=r.content, status=r.status) for r in rows]
        return list(reversed(found))
