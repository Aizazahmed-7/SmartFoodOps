"""Starting and following a turn, for the API layer to call.

Lives here and not in `api/` because it touches the conversation store, and
the layer contract is explicit: routes go through a service, never straight
at a repo. It also owns the background task handles, which is not a thing a
route should be holding either.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from smartfood_otel import get_logger
from smartfood_realtime import Snapshot

from .adapters.conversations import COMPLETE, FAILED, STREAMING, ConversationRepo
from .domain.ports import Message
from .turns import Publisher, frame, run_turn

log = get_logger("ai-assistant.chat")


def _now() -> datetime:
    return datetime.now(UTC)


class ChatService:
    """What the routes are allowed to know about turns.

    The API layer may not import adapters (the layer contract), and it has no
    business owning a task handle either — so starting a turn, snapshotting
    one, and keeping the background tasks alive all live here.
    """

    def __init__(
        self,
        sessions: Any,
        publish: Any,
        build_graph: Any,
        *,
        history: int = 6,
        shown: Any = None,
    ) -> None:
        self._sessions = sessions
        self._publish = publish
        self._build_graph = build_graph
        self._history = history
        self._shown = shown
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(
        self,
        *,
        conversation_id: str,
        message_id: str,
        user_id: str,
        city: str,
        question: str,
    ) -> str | None:
        """The message id this turn will stream on, or None when the
        conversation belongs to somebody else."""
        # One clock reading for the whole turn, and the ANSWER is stamped
        # after the QUESTION even though its row is written first.
        # `created_at` is the conversation's order, not the order rows
        # reached the table — found live, as a history that replayed an
        # answer before its own question.
        asked_at = _now()
        answered_at = asked_at + timedelta(microseconds=1)
        async with self._sessions() as session:
            repo = ConversationRepo(session)
            if not await repo.ensure_conversation(
                conversation_id=conversation_id, user_id=user_id, city=city, now=asked_at
            ):
                # Somebody else's conversation. Refused before a single row
                # is written, so a probe cannot even tell from a side effect
                # that the id exists.
                return None
            await repo.start_message(
                message_id=message_id,
                conversation_id=conversation_id,
                role="assistant",
                content="",
                status=STREAMING,
                now=answered_at,
            )
            asked_id = f"msg_{uuid4().hex}"
            await repo.start_message(
                message_id=asked_id,
                conversation_id=conversation_id,
                role="user",
                content=question,
                status=COMPLETE,
                now=asked_at,
            )
            # History is what came BEFORE this question, so both of this
            # turn's own rows are excluded. Keeping the user row in would
            # send the question to the model twice — once as context, once
            # as the question — and would make every turn look like a reply,
            # which disables the answer cache outright (ADR-0045).
            history = [
                m
                for m in await repo.history(conversation_id=conversation_id, limit=self._history)
                if m.id not in (message_id, asked_id) and m.content
            ]
            await session.commit()

        publisher = Publisher(self._sessions, self._publish, message_id=message_id)
        task = asyncio.create_task(
            run_turn(
                graph=self._build_graph(publisher.emit),
                publisher=publisher,
                sessions=self._sessions,
                message_id=message_id,
                question=question,
                city=city,
                conversation_id=conversation_id,
                user_id=user_id,
                history=tuple(_as_message(m) for m in history),
                shown=self._shown,
            )
        )
        # Held so the loop cannot garbage-collect a running turn mid-answer —
        # asyncio keeps only a weak reference to a bare task.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return message_id

    async def owner(self, message_id: str) -> str | None:
        async with self._sessions() as session:
            return await ConversationRepo(session).owner_of(message_id)

    async def snapshot(self, *, message_id: str, seq_upto: int) -> Snapshot:
        """What a connecting reader has missed, and whether more is coming.

        A finished turn gets a terminal frame APPENDED here, because the one
        the live reader saw was published while this reader was away and
        nothing republishes it. Without it the relay replays the prose and
        then simply stops — and an `EventSource` cannot tell a finished
        answer from a dropped connection, so it reconnects forever.

        The citations come off the row for the same reason: the markers are
        stripped from the text before anybody reads it, so a reader arriving
        late would get the prose with no cards under it (ADR-0042 §5).
        """
        async with self._sessions() as session:
            repo = ConversationRepo(session)
            message = await repo.message(message_id)
            chunks = await repo.chunks_after(message_id=message_id, seq_upto=seq_upto)
        done = message is None or message.status in (COMPLETE, FAILED)
        if done:
            last = chunks[-1][0] if chunks else seq_upto
            item_ids = message.item_ids if message is not None else ()
            chunks = [*chunks, (last + 1, frame(seq=last + 1, done=True, item_ids=item_ids))]
        return Snapshot(chunks=chunks, done=done)


def _as_message(stored: Any) -> Message:
    return Message(role="user" if stored.role == "user" else "assistant", content=stored.content)
