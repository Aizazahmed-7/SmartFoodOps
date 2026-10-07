"""Running a turn detached, and getting its tokens out (ADR-0042).

The ordering under test is write-then-publish. It looks like an
implementation detail and is the difference between a reconnecting reader
finding the chunk it missed and being told it never existed.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.conversations import COMPLETE, FAILED, ConversationRepo
from ai_assistant.db import message_chunks, metadata
from ai_assistant.turns import (
    TRUNCATED,
    UNAVAILABLE,
    Publisher,
    channel_for,
    is_done,
    run_turn,
    seq_of,
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
        self.rows_at_publish: list[int] = []
        # Set by the ordering test to observe the table AT the moment of
        # publish — the only way to prove write-before-publish rather than
        # assert it by reading the code.
        self.count_rows: Callable[[], Awaitable[int]] | None = None

    async def publish(self, channel: str, data: str) -> None:
        if self.count_rows is not None:
            self.rows_at_publish.append(await self.count_rows())
        self.published.append((channel, data))


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


async def _rows(sessions) -> list[tuple[int, str]]:
    async with sessions() as s:
        result = await s.execute(
            sa.select(message_chunks.c.seq, message_chunks.c.content).order_by(message_chunks.c.seq)
        )
        return [(r.seq, r.content) for r in result]


# ── the ordering (ADR-0042 §2) ──────────────────────────────────────


async def test_a_chunk_is_written_before_it_is_published(sessions):
    """THE ordering. A chunk on the bus that is not yet in the table is one
    a reconnecting reader cannot be told about."""
    bus = Bus()

    async def count() -> int:
        return len(await _rows(sessions))

    bus.count_rows = count
    publisher = Publisher(sessions, bus.publish, message_id="m1")
    await publisher.emit("hello")
    await publisher.emit("world")
    # At the moment of each publish, the row was already there.
    assert bus.rows_at_publish == [1, 2]


async def test_sequences_are_monotonic_from_one(sessions):
    publisher = Publisher(sessions, Bus().publish, message_id="m1")
    for text in ("a", "b", "c"):
        await publisher.emit(text)
    assert [seq for seq, _ in await _rows(sessions)] == [1, 2, 3]


async def test_the_published_frame_matches_the_stored_one(sessions):
    """A reader reconstructs from both, so they must be the same bytes."""
    bus = Bus()
    publisher = Publisher(sessions, bus.publish, message_id="m1")
    await publisher.emit("hello")
    stored = (await _rows(sessions))[0][1]
    assert bus.published[0][1] == stored
    assert json.loads(stored) == {"seq": 1, "text": "hello", "done": False}


async def test_the_channel_is_per_message(sessions):
    """A channel carrying a whole conversation would deliver another turn's
    tokens into the middle of this one."""
    bus = Bus()
    await Publisher(sessions, bus.publish, message_id="m1").emit("x")
    assert bus.published[0][0] == channel_for("m1") == "sfo:assist:m1"


async def test_the_terminal_frame_is_published_but_not_stored(sessions):
    """A late reader learns the message finished from its status, not from a
    row — two sources of truth for one fact eventually disagree."""
    bus = Bus()
    publisher = Publisher(sessions, bus.publish, message_id="m1")
    await publisher.emit("hello")
    await publisher.close()
    assert len(await _rows(sessions)) == 1
    assert is_done(bus.published[-1][1])
    assert seq_of(bus.published[-1][1]) == 2


# ── the detached turn ───────────────────────────────────────────────


async def test_a_completed_turn_records_the_assembled_answer(sessions):
    bus = Bus()
    publisher = Publisher(sessions, bus.publish, message_id="m1")

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
    publisher = Publisher(sessions, bus.publish, message_id="m1")
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
    publisher = Publisher(sessions, bus.publish, message_id="m1")
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
    publisher = Publisher(sessions, bus.publish, message_id="m1")
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
    publisher = Publisher(sessions, bus.publish, message_id="m1")
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
    publisher = Publisher(sessions, bus.publish, message_id="m1")
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
    publisher = Publisher(sessions, Bus().publish, message_id="m1")
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
    publisher = Publisher(sessions, bus.publish, message_id="m1")
    await publisher.emit("Try the [item:itm_ab")
    # Only the fragment is held — the prose before it went out at once,
    # because no amount of further text can turn it into a marker.
    assert json.loads(bus.published[0][1])["text"] == "Try the "
    await publisher.flush()
    assert json.loads(bus.published[1][1])["text"] == "[item:itm_ab"
    await publisher.close()
    assert is_done(bus.published[-1][1])


async def test_a_marker_never_reaches_the_reader_or_the_replay_table(sessions):
    """One strip, before the row is written, so a reconnect cannot read
    different text than the reader already saw."""
    bus = Bus()
    publisher = Publisher(sessions, bus.publish, message_id="m1")
    for chunk in (" [item:itm_e8", "d9] R", "aita."):
        await publisher.emit(chunk)
    await publisher.close(item_ids=["itm_raita"])
    live = "".join(json.loads(p)["text"] for _, p in bus.published)
    # No leading space either: the marker opened the answer, and an answer
    # that starts with a blank is a rendering bug in every panel.
    assert live == "Raita."
    assert "".join(json.loads(c)["text"] for _, c in await _rows(sessions)) == "Raita."
    assert json.loads(bus.published[-1][1])["item_ids"] == ["itm_raita"]
