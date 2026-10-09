"""The conversation store (ADR-0042, FR-67, FR-69).

Two properties carry the milestone: a retried POST must not start a second
generation, and a reconnect must be able to find exactly what it missed.
Everything else here is the plumbing those two stand on.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.conversations import (
    COMPLETE,
    FAILED,
    STREAMING,
    ConversationRepo,
)
from ai_assistant.db import conversations, messages, metadata
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _conversation(sessions, conversation_id="c1", city="springfield", now=T0):
    async with sessions() as s:
        await ConversationRepo(s).ensure_conversation(
            conversation_id=conversation_id, user_id="usr_1", city=city, now=now
        )
        await s.commit()


async def _message(sessions, message_id="m1", conversation_id="c1", **over):
    async with sessions() as s:
        created = await ConversationRepo(s).start_message(
            message_id=message_id,
            conversation_id=conversation_id,
            role=over.pop("role", "assistant"),
            content=over.pop("content", ""),
            status=over.pop("status", STREAMING),
            now=over.pop("now", T0),
            **over,
        )
        await s.commit()
    return created


# ── conversations ───────────────────────────────────────────────────


async def test_a_conversation_is_created_once_and_touched_after(sessions):
    await _conversation(sessions)
    await _conversation(sessions, city="islamabad", now=T0 + timedelta(minutes=5))
    async with sessions() as s:
        row = (await s.execute(sa.select(conversations))).mappings().one()
    # The scope is set by the question that STARTED it: letting a later turn
    # move it would mean a follow-up answering from a different city than the
    # one the customer was reading.
    assert row["city"] == "springfield"
    assert row["updated_at"].replace(tzinfo=UTC) == T0 + timedelta(minutes=5)


# ── idempotency (FR-67) ─────────────────────────────────────────────


async def test_messages_without_a_key_never_collide(sessions):
    """Assistant messages carry no key; a unique constraint over NULLs must
    not make the second one vanish."""
    await _conversation(sessions)
    assert await _message(sessions, "m1") is True
    assert await _message(sessions, "m2") is True


async def test_a_finished_message_carries_the_assembled_answer(sessions):
    """`content` is the answer's only durable form: chunks are published and
    never stored, so a conversation reloaded tomorrow reads this row."""
    await _conversation(sessions)
    await _message(sessions)
    async with sessions() as s:
        await ConversationRepo(s).finish_message(
            message_id="m1", content="Try the Raita", status=COMPLETE
        )
        await s.commit()
    async with sessions() as s:
        found = await ConversationRepo(s).message("m1")
    assert found is not None
    assert (found.content, found.status) == ("Try the Raita", COMPLETE)


async def test_a_failed_turn_is_distinguishable_from_a_running_one(sessions):
    await _conversation(sessions)
    await _message(sessions)
    async with sessions() as s:
        await ConversationRepo(s).finish_message(message_id="m1", content="", status=FAILED)
        await s.commit()
    async with sessions() as s:
        found = await ConversationRepo(s).message("m1")
    assert found is not None and found.status == FAILED


async def test_an_unknown_message_is_none_not_an_error(sessions):
    async with sessions() as s:
        assert await ConversationRepo(s).message("nope") is None


# ── history ─────────────────────────────────────────────────────────


async def test_history_is_oldest_first_and_bounded(sessions):
    """Oldest-first because that is prompt order; bounded because an
    unbounded history is a context-cap violation waiting for a chatty
    customer (ADR-0030 §5)."""
    await _conversation(sessions)
    for index in range(5):
        await _message(
            sessions,
            f"m{index}",
            role="user" if index % 2 == 0 else "assistant",
            content=f"turn {index}",
            now=T0 + timedelta(minutes=index),
        )
    async with sessions() as s:
        recent = await ConversationRepo(s).history(conversation_id="c1", limit=3)
    assert [m.content for m in recent] == ["turn 2", "turn 3", "turn 4"]


async def test_history_is_scoped_to_its_conversation(sessions):
    await _conversation(sessions, "c1")
    await _conversation(sessions, "c2")
    await _message(sessions, "m1", "c1", content="mine")
    await _message(sessions, "m2", "c2", content="theirs")
    async with sessions() as s:
        assert [
            m.content for m in await ConversationRepo(s).history(conversation_id="c1", limit=9)
        ] == ["mine"]


# ── retention (NFR-32) ──────────────────────────────────────────────


async def test_purging_a_conversation_takes_its_messages(sessions):
    """The 90-day purge is ONE delete. Dropping the chunk table removed the
    second thing a retention rule had to remember; the CASCADE onto
    `messages` is what is left of it, and it still has to hold."""
    await _conversation(sessions)
    await _message(sessions)

    async with sessions() as s:
        await s.execute(sa.delete(conversations).where(conversations.c.id == "c1"))
        await s.commit()

    async with sessions() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(messages)) == 0
