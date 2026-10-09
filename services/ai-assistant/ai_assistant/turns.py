"""Running a turn, and getting its tokens to a reader (ADR-0031, ADR-0042).

The turn is a background task, not a request handler. That is what makes the
stream resumable: a generation that dies with its socket cannot be rejoined,
so the socket is not allowed to own it. A reader who never connects,
disconnects, or reconnects three times changes nothing about what is being
written.

Chunks are published and never stored. There is no resume: a reader who
disconnects mid-answer has lost the stream, and the durable record is the
assembled `messages` row the turn writes at the end.

That makes one thing load-bearing which used to be covered by the replay —
the bus is pub/sub, so a frame published before the reader subscribes is
DROPPED. `Publisher` therefore waits for a reader before its first frame
(`wait_for_reader`), because a refusal and an empty retrieval are answered
with no model at all and would otherwise be published into an empty room.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from smartfood_otel import get_logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .adapters.conversations import COMPLETE, FAILED, ConversationRepo
from .domain.graph import TurnState
from .domain.graph.turn import TurnRunner
from .domain.grounding import Stripper
from .metrics import SAFETY_REFUSALS, UNGROUNDED

log = get_logger("ai-assistant.turns")


def channel_for(message_id: str) -> str:
    """One channel per MESSAGE, not per conversation: a reader is following
    one answer being written, and a channel that carried a whole conversation
    would deliver another turn's tokens into the middle of this one."""
    return f"sfo:assist:{message_id}"


def frame(*, text: str = "", done: bool = False, item_ids: Sequence[str] = ()) -> str:
    """One SSE payload. `item_ids` rides the TERMINAL frame only — they are
    known once grounding has run, and a client renders cards from them
    (priced live, FR-60) rather than from anything in the prose.

    No sequence number: it existed so a reconnecting reader could drop what
    it already held, and there is no reconnect to serve.
    """
    body: dict[str, Any] = {"text": text, "done": done}
    if item_ids:
        body["item_ids"] = list(item_ids)
    return json.dumps(body)


def is_done(raw: str) -> bool:
    return bool(json.loads(raw).get("done"))


class Publisher:
    """Publish. Nothing else.

    No database: chunks are not stored, so the only durable write a turn
    makes is the assembled row at the end. It keeps `text` because two
    decisions need to know whether the reader has words on screen already —
    which apology to send on failure, and whether the graph's answer still
    has to be emitted.
    """

    def __init__(
        self,
        publish: Callable[[str, str], Awaitable[None]],
        *,
        message_id: str,
        ready: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._publish = publish
        self._message_id = message_id
        self._ready = ready
        self._waited = False
        self._stripper = Stripper()
        self.text: list[str] = []

    async def _attached(self) -> None:
        """Wait for a reader, ONCE, before anything goes on the bus.

        Lazy rather than at the top of the turn: by the time a generated
        answer produces its first token the reader has long since arrived,
        so this costs nothing on the common path and pays only where it
        matters — the model-free answers that are ready immediately.
        """
        if self._waited:
            return
        self._waited = True
        if self._ready is not None:
            await self._ready(channel_for(self._message_id))

    async def emit(self, text: str) -> None:
        """Strip, then write, then publish.

        Stripping here rather than at the graph's edge is what keeps the
        replay and the live stream identical: one pass, before the row is
        written, so a reconnect cannot read different text than the reader
        already saw.
        """
        safe = self._stripper.push(text)
        if not safe:
            return  # the whole chunk was a marker, or the start of one
        await self._write(safe)

    async def _write(self, text: str) -> None:
        await self._attached()
        self.text.append(text)
        await self._publish(channel_for(self._message_id), frame(text=text))

    async def flush(self) -> None:
        """Send whatever the stripper is still holding.

        Separate from `close` so the residue reaches the reader BEFORE the
        terminal frame tells it the answer is over. It also has to land
        before `finish_message`, because `publisher.text` is what the stored
        content falls back to — a tail still inside the stripper is a tail
        missing from the durable row.
        """
        residue = self._stripper.flush()
        if residue:
            await self._write(residue)

    async def close(self, item_ids: Sequence[str] = ()) -> None:
        """Publish the terminal frame, on every path including failure.

        `_attached` again, and not redundantly: a turn that refused before
        emitting anything reaches here having never called `_write`, and the
        terminal frame is then the only thing the reader will ever get. It
        is idempotent, so the ordinary path pays nothing.

        Without this frame an `EventSource` cannot tell a finished answer
        from a dropped connection, and reconnects forever.
        """
        await self._attached()
        await self._publish(channel_for(self._message_id), frame(done=True, item_ids=item_ids))


TRUNCATED = " …sorry, I couldn't finish that — please ask again."
"""Appended when a turn fails AFTER it has already streamed prose.

`UNAVAILABLE` cannot serve here: the reader has words on screen, and
replacing them would rewrite what they just read. But saying nothing is
worse — the answer simply stops mid-word and the terminal frame arrives, so
a truncated failure is indistinguishable from a finished answer. Found by
the B3 review, which noted `UNAVAILABLE` was only ever reachable for
failures BEFORE the first token, the less common case.
"""

CITATIONS_ONLY = "Here are a few dishes from the menu that match."
"""When grounding leaves no prose at all.

The model sometimes emits markers INSTEAD of dish names (ADR-0043's
amendment records it), and stripping them then yields an empty string. That
shipped as a blank bubble which the KPI recorded as `answered` with cited
items — a turn that rendered nothing and counted as a success. Naming no
dish is deliberate: the cards carry the names, priced live (FR-60).
"""

UNAVAILABLE = (
    "Sorry — I couldn't finish that answer just now. Please try again in a "
    "moment, or browse the menu directly."
)
"""What a reader sees when the turn broke.

Fixed text, for the same reason `REFUSAL` is: it costs no provider call, and
the provider is exactly what is failing. Without it a failed turn closes the
stream having emitted NOTHING — a blank bubble that appears and then stops,
which reads as the app being broken rather than the model being busy. Found
live on the first real generation, behind a provider 503.
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _why(exc: BaseException) -> str:
    """The whole cause chain, not just the outermost message.

    `NoProviderAvailable("every provider failed for task generate")` says
    nothing about WHY — a timeout, a rate limit and a bad model id all
    surface identically, and the one fact that distinguishes them is the
    exception it was raised `from`. Types included: a bare message is often
    empty on transport errors.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _fact(
    *,
    message_id: str,
    conversation_id: str,
    user_id: str,
    city: str,
    outcome: str,
    started: float,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One interaction, as the fact analytics projects (FR-94).

    ABSOLUTE values keyed by `message_id`, never deltas. The outbox is
    at-least-once and the poller re-sends anything it published but did not
    mark, so a fact that said "+1 question" would inflate the KPI every time
    a poller crashed between the two. A fact that says what this interaction
    WAS converges no matter how often it arrives.

    The question text is NOT here. A KPI needs counts and ids; it does not
    need what a customer typed, and shipping free text into an analytics
    store puts NFR-32's retention rule on a second copy nobody remembers
    (ADR-0043 §6 keeps identities out of prompts for the same reason).
    """
    state = state or {}
    return {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "city": city,
        "outcome": outcome,
        "refusal_reason": str(state.get("refusal_reason", "none")),
        # "" when a model produced it. Without this, `duration_ms` averages a
        # 4ms cache hit against a 2s generation and FR-95's "average AI
        # response time" stops describing anything.
        # What the answer CITED, not what retrieval found: a conversion
        # credited to a restaurant the customer was never shown is invented.
        "item_ids": list(state.get("item_ids", ())),
        "restaurant_ids": list(state.get("restaurant_ids", ())),
        "candidates": len(state.get("candidates", ())),
        "ungrounded": int(state.get("dropped", 0)),
        # FR-95's "average AI response time" — measured over the whole turn,
        # retrieval included, because that is the wait a customer sits
        # through. A provider-only number would flatter us.
        "duration_ms": round((perf_counter() - started) * 1000, 1),
    }


async def _settle_failed(
    *,
    publisher: Publisher,
    sessions: async_sessionmaker[AsyncSession],
    message_id: str,
    conversation_id: str,
    user_id: str,
    city: str,
    started: float,
) -> None:
    """Mark a turn failed, tell the reader, and record the fact.

    Shared by the ordinary failure path and the cancellation path, because a
    turn that died at shutdown is exactly as unfinished as one whose provider
    fell over, and the row must not be able to tell them apart by accident.
    """
    # Two different failures, and they read differently to a customer.
    # Nothing streamed yet: send the whole apology. Something already on
    # screen: say it stopped, and keep what they read — replacing it would
    # rewrite a sentence in front of them.
    await publisher.emit(TRUNCATED if publisher.text else UNAVAILABLE)
    await publisher.flush()
    said = "".join(publisher.text)
    async with sessions() as session:
        repo = ConversationRepo(session)
        # A failure is a fact too, and a KPI that only counted successes
        # would improve every time the provider got worse.
        if await repo.finish_message(message_id=message_id, content=said, status=FAILED):
            await repo.stage_interaction(
                message_id=message_id,
                payload=_fact(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    city=city,
                    outcome="failed",
                    started=started,
                ),
                now=_now(),
            )
        await session.commit()


async def run_turn(
    *,
    graph: TurnRunner,
    publisher: Publisher,
    sessions: async_sessionmaker[AsyncSession],
    message_id: str,
    question: str,
    city: str,
    conversation_id: str = "",
    user_id: str = "",
    history: tuple = (),
    shown: Any = None,
) -> None:
    """Drive one turn to completion and record what happened.

    Exceptions are caught rather than raised: this runs detached, so there is
    no caller to receive them. A failure marks the message `failed` and
    closes the stream — a reader watching a dead generation forever is worse
    than one told it broke.
    """
    # Declared before the try so the `finally` can close the stream with
    # them: a turn that failed cites nothing, and the terminal frame still
    # has to be published.
    item_ids: Sequence[str] = ()
    started = perf_counter()
    try:
        state = await graph.ainvoke(TurnState(question=question, city=city, history=history))
        answer = str(state.get("answer", ""))
        item_ids = list(state.get("item_ids", ()))
        if state.get("refusal_reason", "none") != "none":
            SAFETY_REFUSALS.labels(reason=str(state["refusal_reason"])).inc()
        dropped = int(state.get("dropped", 0))
        if dropped:
            UNGROUNDED.inc(dropped)
        # A refusal and a no-match produce an answer no token ever carried,
        # so the reader is sent it here — otherwise the panel would show an
        # empty bubble and then close.
        if not publisher.text:
            await publisher.emit(answer or CITATIONS_ONLY)
        # Everything the stripper is still holding, written BEFORE the row
        # is settled — a reader who snapshots after `complete` must find the
        # whole answer already there.
        await publisher.flush()
        answer = answer or "".join(publisher.text)
        outcome = str(state.get("stopped", "")) or "answered"
        async with sessions() as session:
            repo = ConversationRepo(session)
            settled = await repo.finish_message(
                message_id=message_id, content=answer, status=COMPLETE, item_ids=item_ids
            )
            # The fact goes in the SAME transaction as the row it describes,
            # and only when this call is the one that settled it — that is
            # the whole of ADR-0002's guarantee here. A turn replayed after a
            # crash loses the guarded UPDATE and stages nothing.
            if settled:
                await repo.stage_interaction(
                    message_id=message_id,
                    payload=_fact(
                        message_id=message_id,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        city=city,
                        outcome=outcome,
                        started=started,
                        state=state,
                    ),
                    now=_now(),
                )
            await session.commit()
        # An answer's citations are dishes we put in front of a customer,
        # so they belong in the same acceptance denominator as the panel's
        # list (FR-79). Recorded after the row is settled and outside its
        # transaction: the answer is already correct and already streamed,
        # and a failure to note it must not undo that.
        # Guarded by `settled`, like the interaction fact beside it: a
        # re-entered turn must not record the same dishes in the acceptance
        # denominator twice. No replay path reaches here today, but the
        # guard is free and the fact next to it already has one.
        if settled and shown is not None and item_ids:
            await shown(user_id, city, item_ids)
        log.info(
            "turn complete",
            message_id=message_id,
            items=len(item_ids),
            ungrounded=dropped,
        )
    except asyncio.CancelledError:
        # Shutdown, not a bug — but the row still has to be settled. A turn
        # killed mid-generation used to leave `streaming` behind forever, so
        # every later reader snapshotted `done=False`, subscribed to a
        # channel nobody would publish on, and reconnected for good; the KPI
        # lost one answer per deploy with no way to see it in the number
        # (B3 review). Best-effort: the engine may already be going away,
        # and failing to record a shutdown must not mask the shutdown.
        with suppress(Exception):
            await _settle_failed(
                publisher=publisher,
                sessions=sessions,
                message_id=message_id,
                conversation_id=conversation_id,
                user_id=user_id,
                city=city,
                started=started,
            )
        raise
    except Exception as exc:
        log.error("turn failed", message_id=message_id, error=_why(exc))
        # Stored as well as streamed: the row is what a reloaded conversation
        # reads, so an empty one would put the blank bubble back one layer
        # down. `status` is what says this was a failure, not the text.
        await _settle_failed(
            publisher=publisher,
            sessions=sessions,
            message_id=message_id,
            conversation_id=conversation_id,
            user_id=user_id,
            city=city,
            started=started,
        )
    finally:
        await publisher.close(item_ids)
