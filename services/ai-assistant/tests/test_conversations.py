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
from ai_assistant.db import conversations, message_chunks, messages, metadata
from sqlalchemy.exc import IntegrityError
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


async def test_a_retried_post_does_not_start_a_second_generation(sessions):
    """The row IS the idempotency record (ADR-0024). Two generations for one
    question is a second provider bill and two different answers."""
    await _conversation(sessions)
    assert await _message(sessions, "m1", role="user", idempotency_key="k1") is True
    assert await _message(sessions, "m2", role="user", idempotency_key="k1") is False
    async with sessions() as s:
        count = await s.scalar(sa.select(sa.func.count()).select_from(messages))
    assert count == 1


async def test_the_same_key_in_another_conversation_is_a_different_message(sessions):
    """Keys are scoped to a conversation — two customers can pick the same
    client-generated key without colliding."""
    await _conversation(sessions, "c1")
    await _conversation(sessions, "c2")
    assert await _message(sessions, "m1", "c1", role="user", idempotency_key="k1") is True
    assert await _message(sessions, "m2", "c2", role="user", idempotency_key="k1") is True


async def test_messages_without_a_key_never_collide(sessions):
    """Assistant messages carry no key; a unique constraint over NULLs must
    not make the second one vanish."""
    await _conversation(sessions)
    assert await _message(sessions, "m1") is True
    assert await _message(sessions, "m2") is True


# ── the replay buffer (FR-69) ───────────────────────────────────────


async def test_a_reconnect_reads_exactly_what_it_missed(sessions):
    await _conversation(sessions)
    await _message(sessions)
    async with sessions() as s:
        repo = ConversationRepo(s)
        for seq, text in [(1, "Try "), (2, "the "), (3, "Raita")]:
            await repo.append_chunk(message_id="m1", seq=seq, content=text, now=T0)
        await s.commit()

    async with sessions() as s:
        repo = ConversationRepo(s)
        assert await repo.chunks_after(message_id="m1", seq_upto=0) == [
            (1, "Try "),
            (2, "the "),
            (3, "Raita"),
        ]
        # A reader that already has 2 gets only what follows — the `seq`
        # filter is what pays for subscribing before snapshotting.
        assert await repo.chunks_after(message_id="m1", seq_upto=2) == [(3, "Raita")]
        assert await repo.chunks_after(message_id="m1", seq_upto=9) == []


async def test_a_reused_seq_is_refused_by_the_database(sessions):
    """A producer that reused a seq would corrupt a reconnect silently, so
    the composite PK refuses instead of trusting the caller."""
    await _conversation(sessions)
    await _message(sessions)
    async with sessions() as s:
        await ConversationRepo(s).append_chunk(message_id="m1", seq=1, content="a", now=T0)
        await s.commit()
    with pytest.raises(IntegrityError):  # the PK, not a hopeful catch-all
        async with sessions() as s:
            await ConversationRepo(s).append_chunk(message_id="m1", seq=1, content="b", now=T0)
            await s.commit()


async def test_a_finished_message_carries_the_assembled_answer(sessions):
    """Nothing downstream reconstructs a message by concatenating chunks
    (ADR-0042 §6) — the status tells a reconnect to replay and close."""
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


async def test_purging_a_conversation_takes_its_messages_and_chunks(sessions):
    """The 90-day purge is ONE delete. A retention rule that needs someone to
    remember a second table is a rule that fails an audit, not a test."""
    await _conversation(sessions)
    await _message(sessions)
    async with sessions() as s:
        await ConversationRepo(s).append_chunk(message_id="m1", seq=1, content="a", now=T0)
        await s.commit()

    async with sessions() as s:
        await s.execute(sa.delete(conversations).where(conversations.c.id == "c1"))
        await s.commit()

    async with sessions() as s:
        assert await s.scalar(sa.select(sa.func.count()).select_from(messages)) == 0
        assert await s.scalar(sa.select(sa.func.count()).select_from(message_chunks)) == 0
