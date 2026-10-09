"""Running a turn detached, and getting its tokens out.

Chunks are published and never stored, so there is no replay to verify.
What replaces it is the wait: the bus drops a frame published into a
channel nobody has subscribed to, and a refusal is ready before the
browser has finished reading the response that told it where to listen.
"""

import json
from datetime import UTC, datetime

import pytest
from ai_assistant.adapters.conversations import COMPLETE, FAILED, ConversationRepo
from ai_assistant.db import metadata
from ai_assistant.turns import (
    TRUNCATED,
    UNAVAILABLE,
    Publisher,
    channel_for,
    is_done,
    run_turn,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        repo = ConversationRepo(s)
        await repo.ensure_conversation(
            conversation_id="c1", user_id="usr_1", city="springfield", now=T0
        )
        await repo.start_message(
            message_id="m1",
            conversation_id="c1",
            role="assistant",
            content="",
            status="streaming",
            now=T0,
        )
        await s.commit()
    yield maker
    await engine.dispose()


class Bus:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, data))


class Reader:
    """A `ready` hook that records when it was asked and what for."""

    def __init__(self) -> None:
        self.waited_for: list[str] = []

    async def __call__(self, channel: str) -> bool:
        self.waited_for.append(channel)
        return True


class Graph:
    def __init__(self, state=None, raises: BaseException | None = None):
        # BaseException, not Exception: one test raises CancelledError to
        # prove shutdown can still stop a turn.
        self.state = state or {"answer": "Try the Raita.", "item_ids": ["itm_raita"]}
        self.raises = raises

    async def ainvoke(self, state):
        if self.raises:
            raise self.raises
        return self.state


# ── publishing, and waiting for somebody to publish TO ──────────────


async def test_nothing_is_published_before_a_reader_is_attached():
    """The replacement for the stored replay. Pub/sub drops a frame sent
    into an empty channel, so the wait has to happen BEFORE the first
    publish — not beside it, and not after."""
    bus, ready = Bus(), Reader()
    publisher = Publisher(bus.publish, message_id="m1", ready=ready)
    assert ready.waited_for == []  # constructing one waits for nothing
    await publisher.emit("hello")
    assert ready.waited_for == ["sfo:assist:m1"]
    assert len(bus.published) == 1


async def test_the_reader_is_waited_for_exactly_once():
    """Per turn, not per token. A poll on every frame would put the bus in
    front of every chunk of every answer."""
    bus, ready = Bus(), Reader()
    publisher = Publisher(bus.publish, message_id="m1", ready=ready)
    for text in ("a", "b", "c"):
        await publisher.emit(text)
    await publisher.close()
    assert ready.waited_for == ["sfo:assist:m1"]


async def test_a_turn_that_only_refuses_still_waits_before_the_terminal_frame():
    """THE case this exists for. A safety refusal emits nothing through the
    graph, so `close()` is the first and only thing on the bus — and it is
    published in under a millisecond, before any browser could have
    subscribed. Without the wait in `close` too, the reader gets silence and
    heartbeats to the lifetime."""
    bus, ready = Bus(), Reader()
    await Publisher(bus.publish, message_id="m1", ready=ready).close()
    assert ready.waited_for == ["sfo:assist:m1"]
    assert is_done(bus.published[-1][1])


async def test_a_publisher_with_no_ready_hook_simply_publishes():
    """A test (or a deployment with no bus) must not have to supply one."""
    bus = Bus()
    await Publisher(bus.publish, message_id="m1").emit("hello")
    assert len(bus.published) == 1


async def test_the_frame_carries_text_and_no_sequence_number():
    """`seq` existed so a reconnecting reader could drop what it already
    held. There is no reconnect, so a sequence number would be a field
    nothing reads and one more thing to keep consistent."""
    bus = Bus()
    await Publisher(bus.publish, message_id="m1").emit("hello")
    assert json.loads(bus.published[0][1]) == {"text": "hello", "done": False}


async def test_the_channel_is_per_message():
    """A channel carrying a whole conversation would deliver another turn's
    tokens into the middle of this one."""
    bus = Bus()
    await Publisher(bus.publish, message_id="m1").emit("x")
    assert bus.published[0][0] == channel_for("m1") == "sfo:assist:m1"


async def test_the_terminal_frame_carries_the_citations():
    """A client renders cards from these, priced live — never from the
    prose, which has had its markers stripped."""
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    await publisher.emit("hello")
    await publisher.close(["itm_a"])
    assert is_done(bus.published[-1][1])
    assert json.loads(bus.published[-1][1])["item_ids"] == ["itm_a"]


# ── the detached turn ───────────────────────────────────────────────


async def test_a_completed_turn_records_the_assembled_answer(sessions):
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")

    async def emitting(state):
        await publisher.emit("Try the ")
        await publisher.emit("Raita.")
        return {"answer": "Try the Raita.", "item_ids": ["itm_raita"], "dropped": 0}

    graph = Graph()
    graph.ainvoke = emitting
    await run_turn(
        graph=graph,
        publisher=publisher,
        sessions=sessions,
        message_id="m1",
        question="something light?",
        city="springfield",
    )
    async with sessions() as s:
        message = await ConversationRepo(s).message("m1")
    assert message is not None
    assert message.content == "Try the Raita." and message.status == COMPLETE


async def test_an_answer_no_token_carried_is_still_sent(sessions):
    """A refusal and a no-match are produced without a model, so nothing was
    emitted — without this the panel shows an empty bubble and closes."""
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    await run_turn(
        graph=Graph({"answer": "I can't advise on allergies.", "refusal_reason": "allergen"}),
        publisher=publisher,
        sessions=sessions,
        message_id="m1",
        question="I'm allergic, safe?",
        city="springfield",
    )
    assert json.loads(bus.published[0][1])["text"] == "I can't advise on allergies."


async def test_a_failed_turn_marks_the_message_and_closes_the_stream(sessions):
    """It runs detached, so there is no caller to raise at. A reader watching
    a dead generation forever is worse than one told it broke."""
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    await run_turn(
        graph=Graph(raises=RuntimeError("provider down")),
        publisher=publisher,
        sessions=sessions,
        message_id="m1",
        question="what's good?",
        city="springfield",
    )
    async with sessions() as s:
        message = await ConversationRepo(s).message("m1")
    assert message is not None and message.status == FAILED
    assert is_done(bus.published[-1][1])
    # Not a blank bubble: the reader is TOLD it broke, in both places they
    # could read it from — the live stream and the stored row.
    assert json.loads(bus.published[0][1])["text"] == UNAVAILABLE
    assert message.content == UNAVAILABLE


async def test_a_turn_that_fails_mid_answer_keeps_what_it_already_said(sessions):
    """The apology REPLACES nothing. A generation that broke after two
    sentences has already put them on the reader's screen, and re-emitting
    over them would make the panel contradict itself."""
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    await publisher.emit("Raita is light. ")  # the graph got this far
    await run_turn(
        graph=Graph(raises=RuntimeError("provider died")),
        publisher=publisher,
        sessions=sessions,
        message_id="m1",
        question="what's good?",
        city="springfield",
    )
    # What they already read is kept, and they are TOLD it stopped —
    # without the suffix a truncated failure is indistinguishable from a
    # finished answer, because the terminal frame arrives either way.
    texts = [json.loads(p)["text"] for _, p in bus.published if json.loads(p)["text"]]
    # The stripper collapses TRUNCATED's leading space against the one the
    # answer already ended on — the same seam it closes for markers.
    assert texts == ["Raita is light. ", TRUNCATED.lstrip()]
    async with sessions() as s:
        stored = await ConversationRepo(s).message("m1")
    assert stored is not None and stored.status == FAILED
    # The row and the chunks agree: a reload must not lose the sentence the
    # customer just read.
    assert stored.content == "Raita is light. " + TRUNCATED.lstrip()


async def test_the_stream_is_closed_even_when_the_turn_succeeds(sessions):
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    await run_turn(
        graph=Graph(),
        publisher=publisher,
        sessions=sessions,
        message_id="m1",
        question="x",
        city="c",
    )
    assert is_done(bus.published[-1][1])


async def test_cancellation_is_not_swallowed(sessions):
    """Shutdown must be able to stop a turn. Catching CancelledError with
    everything else would make the task unkillable."""
    import asyncio

    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    with pytest.raises(asyncio.CancelledError):
        await run_turn(
            graph=Graph(raises=asyncio.CancelledError()),
            publisher=publisher,
            sessions=sessions,
            message_id="m1",
            question="x",
            city="c",
        )


async def test_an_ungrounded_citation_is_counted(sessions):
    """`assistant_ungrounded_total` is the alerting signal for a model that
    is inventing dishes (NFR-26), so the turn has to actually record it."""
    from ai_assistant.metrics import UNGROUNDED

    before = UNGROUNDED._value.get()
    publisher = Publisher(Bus().publish, message_id="m1")
    await run_turn(
        graph=Graph({"answer": "Try it.", "item_ids": [], "dropped": 2}),
        publisher=publisher,
        sessions=sessions,
        message_id="m1",
        question="x",
        city="c",
    )
    assert UNGROUNDED._value.get() == before + 2


async def test_closing_flushes_text_held_behind_an_unfinished_marker(sessions):
    """A model that stopped mid-marker left real characters in the buffer.
    Closing without flushing would silently drop the end of the answer."""
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    await publisher.emit("Try the [item:itm_ab")
    # Only the fragment is held — the prose before it went out at once,
    # because no amount of further text can turn it into a marker.
    assert json.loads(bus.published[0][1])["text"] == "Try the "
    await publisher.flush()
    assert json.loads(bus.published[1][1])["text"] == "[item:itm_ab"
    await publisher.close()
    assert is_done(bus.published[-1][1])


async def test_a_marker_never_reaches_the_reader():
    """One strip, on the way out, so the opaque id the model cites is never
    something a customer sees."""
    bus = Bus()
    publisher = Publisher(bus.publish, message_id="m1")
    for chunk in (" [item:itm_e8", "d9] R", "aita."):
        await publisher.emit(chunk)
    await publisher.close(item_ids=["itm_raita"])
    live = "".join(json.loads(p)["text"] for _, p in bus.published)
    # No leading space either: the marker opened the answer, and an answer
    # that starts with a blank is a rendering bug in every panel.
    assert live == "Raita."
    assert json.loads(bus.published[-1][1])["item_ids"] == ["itm_raita"]
