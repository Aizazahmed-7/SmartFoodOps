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

from .adapters.conversations import COMPLETE, STREAMING, ConversationRepo
from .domain.ports import Message
from .turns import Publisher, run_turn

log = get_logger("ai-assistant.chat")


def _now() -> datetime:
    return datetime.now(UTC)


class ChatService:
    """What the routes are allowed to know about turns.

    The API layer may not import adapters (the layer contract), and it has no
    business owning a task handle either — so starting a turn and keeping
    its background task alive both live here.
    """

    def __init__(
        self,
        sessions: Any,
        publish: Any,
        build_graph: Any,
        *,
        history: int = 6,
        shown: Any = None,
        ready: Any = None,
    ) -> None:
        self._sessions = sessions
        self._publish = publish
        self._build_graph = build_graph
        self._history = history
        self._shown = shown
        self._ready = ready
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
            # send the question to the model twice — once as context and
            # once as the question — and the assistant row is still empty,
            # which would put a blank turn in front of the model.
            history = [
                m
                for m in await repo.history(conversation_id=conversation_id, limit=self._history)
                if m.id not in (message_id, asked_id) and m.content
            ]
            await session.commit()

        publisher = Publisher(self._publish, message_id=message_id, ready=self._ready)
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


def _as_message(stored: Any) -> Message:
    return Message(role="user" if stored.role == "user" else "assistant", content=stored.content)
