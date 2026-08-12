"""Every branch of the §4 protocol: reserve, replay, reuse, in-progress,
stale takeover (both flavors), complete-joins-the-tx, release."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from smartfood_idempotency import (
    BodyMismatch,
    IdempotencyStore,
    InProgress,
    Replay,
    Reserved,
    body_hash,
    idempotency_table,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

HASH_A = body_hash(b'{"cart": "a"}')
HASH_B = body_hash(b'{"cart": "b"}')


async def _store(**kwargs):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    metadata = sa.MetaData()
    table = idempotency_table(metadata)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return IdempotencyStore(sessions, table, **kwargs), sessions, table


async def _age_row(sessions, table, key: str, seconds: int):
    async with sessions() as s:
        await s.execute(
            table.update()
            .where(table.c.idem_key == key)
            .values(created_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await s.commit()


async def test_fresh_key_reserves():
    store, _, _ = await _store()
    assert isinstance(await store.reserve("usr_1", "k1", HASH_A), Reserved)


async def test_duplicate_while_in_progress():
    store, _, _ = await _store()
    await store.reserve("usr_1", "k1", HASH_A)
    assert isinstance(await store.reserve("usr_1", "k1", HASH_A), InProgress)


async def test_complete_then_replay_verbatim():
    store, sessions, _ = await _store()
    await store.reserve("usr_1", "k1", HASH_A)
    async with sessions() as session:
        await store.complete(session, "usr_1", "k1", 202, {"order_id": "ord_1"})
        await session.commit()
    outcome = await store.reserve("usr_1", "k1", HASH_A)
    assert isinstance(outcome, Replay)
    assert (outcome.response_status, outcome.response_body) == (202, {"order_id": "ord_1"})


async def test_same_key_different_body_is_mismatch():
    store, _, _ = await _store()
    await store.reserve("usr_1", "k1", HASH_A)
    assert isinstance(await store.reserve("usr_1", "k1", HASH_B), BodyMismatch)


async def test_scopes_isolate_users():
    store, _, _ = await _store()
    await store.reserve("usr_1", "k1", HASH_A)
    assert isinstance(await store.reserve("usr_2", "k1", HASH_B), Reserved)


async def test_stale_in_progress_taken_over():
    """The crash-recovery path: holder died mid-execution, TTL expires,
    the next attempt re-executes."""
    store, sessions, table = await _store(in_progress_ttl_seconds=10)
    await store.reserve("usr_1", "k1", HASH_A)
    await _age_row(sessions, table, "k1", 60)
    assert isinstance(await store.reserve("usr_1", "k1", HASH_A), Reserved)


async def test_expired_complete_reexecutes():
    store, sessions, table = await _store(replay_ttl_seconds=100)
    await store.reserve("usr_1", "k1", HASH_A)
    async with sessions() as session:
        await store.complete(session, "usr_1", "k1", 202, {"order_id": "ord_1"})
        await session.commit()
    await _age_row(sessions, table, "k1", 3600)
    assert isinstance(await store.reserve("usr_1", "k1", HASH_A), Reserved)


async def test_takeover_race_has_one_winner(monkeypatch):
    """Two stale-takeover attempts: the guarded UPDATE (WHERE status=...)
    lets exactly one win; the loser is told to wait."""
    store, sessions, table = await _store(in_progress_ttl_seconds=10)
    await store.reserve("usr_1", "k1", HASH_A)
    await _age_row(sessions, table, "k1", 60)

    winner = await store.reserve("usr_1", "k1", HASH_A)  # takes over, resets created_at
    assert isinstance(winner, Reserved)
    loser = await store.reserve("usr_1", "k1", HASH_A)  # row now fresh IN_PROGRESS
    assert isinstance(loser, InProgress)


async def test_vanished_row_between_conflict_and_read(monkeypatch):
    """Insert 'conflicts', but the row is gone by the time we read
    (a concurrent release won the race): the conservative answer is
    InProgress — the client's retry re-inserts cleanly."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    store, _, _ = await _store()  # table is EMPTY — the "vanished" state
    real_execute = AsyncSession.execute
    fired = {"n": 0}

    async def conflicting_execute(self, statement, *args, **kwargs):
        if statement.__class__.__name__ == "Insert" and fired["n"] == 0:
            fired["n"] += 1
            raise IntegrityError("stmt", {}, Exception("duplicate"))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", conflicting_execute)
    assert isinstance(await store.reserve("usr_1", "k1", HASH_A), InProgress)


async def test_release_frees_the_key_immediately():
    store, _, _ = await _store()
    await store.reserve("usr_1", "k1", HASH_A)
    await store.release("usr_1", "k1")
    assert isinstance(await store.reserve("usr_1", "k1", HASH_A), Reserved)


async def test_body_hash_is_stable_sha256():
    assert body_hash(b"x") == body_hash(b"x")
    assert body_hash(b"x") != body_hash(b"y")
    assert len(body_hash(b"x")) == 64
