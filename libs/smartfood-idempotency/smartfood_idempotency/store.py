"""The api-standards §4 idempotency protocol, as one audited implementation.

Reserve-first: the INSERT is the lock. Every verdict a caller can receive
is a value (Reserved/Replay/InProgress/BodyMismatch), not an exception —
the route maps them to 2xx/409/422; only Reserved proceeds to execute.

complete() runs on the CALLER'S session, inside the caller's transaction:
the stored response commits atomically with the business write. A crash
between "order committed" and "response stored" is therefore impossible —
which is exactly the gap that would otherwise re-execute a placement after
the IN_PROGRESS takeover TTL.

release() is for DETERMINISTIC business failures (409 PRICE_CHANGED etc.):
the key frees immediately instead of squatting until the TTL — the client
will retry with a fresh key and a fresh body anyway.

PG-only by decision (plan §cross-cutting): the PG row is the arbiter either
way; the Redis fast path is a prod latency optimization, noted and deferred.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class Reserved:
    """You hold the key — execute, then complete() inside your tx."""


@dataclass(frozen=True)
class Replay:
    response_status: int
    response_body: dict[str, Any]


@dataclass(frozen=True)
class InProgress:
    """Another request with this key is executing right now."""


@dataclass(frozen=True)
class BodyMismatch:
    """Same key, different body — a client bug (422 IDEMPOTENCY_KEY_REUSE)."""


ReserveOutcome = Reserved | Replay | InProgress | BodyMismatch


def body_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """sqlite returns naive datetimes; TTL math needs aware."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class IdempotencyStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        table: sa.Table,
        *,
        replay_ttl_seconds: int = 86_400,  # 24h — api-standards §4
        in_progress_ttl_seconds: int = 300,  # crash-recovery takeover window
    ):
        self._sessions = sessions
        self._t = table
        self._replay_ttl = timedelta(seconds=replay_ttl_seconds)
        self._in_progress_ttl = timedelta(seconds=in_progress_ttl_seconds)

    async def reserve(self, scope: str, key: str, request_hash: str) -> ReserveOutcome:
        """Step 1 of the protocol — its own SHORT transaction, committed
        before any business work begins (the reservation must be visible to
        concurrent duplicates immediately)."""
        async with self._sessions() as session:
            try:
                await session.execute(
                    self._t.insert().values(
                        scope=scope,
                        idem_key=key,
                        body_hash=request_hash,
                        status="IN_PROGRESS",
                        created_at=_now(),
                    )
                )
                await session.commit()
                return Reserved()
            except IntegrityError:
                await session.rollback()  # PG aborts the tx on conflict

            row = (
                await session.execute(
                    sa.select(self._t).where(
                        (self._t.c.scope == scope) & (self._t.c.idem_key == key)
                    )
                )
            ).one_or_none()
            if row is None:
                # Vanished between our conflict and this read (a concurrent
                # release). Conservative answer; the client's retry re-inserts.
                return InProgress()
            if row.body_hash != request_hash:
                return BodyMismatch()
            age = _now() - _aware(row.created_at)
            if row.status == "COMPLETE":
                if age > self._replay_ttl:
                    return await self._take_over(session, scope, key, request_hash, "COMPLETE")
                return Replay(response_status=row.response_status, response_body=row.response_body)
            # IN_PROGRESS: fresh → wait; stale → the holder crashed, take over.
            if age > self._in_progress_ttl:
                return await self._take_over(session, scope, key, request_hash, "IN_PROGRESS")
            return InProgress()

    async def _take_over(
        self, session: AsyncSession, scope: str, key: str, request_hash: str, expected: str
    ) -> ReserveOutcome:
        """Guarded reset of an expired row: WHERE status=:expected referees
        concurrent take-over attempts — exactly one wins, losers wait."""
        result = await session.execute(
            self._t.update()
            .where(
                (self._t.c.scope == scope)
                & (self._t.c.idem_key == key)
                & (self._t.c.status == expected)
            )
            .values(
                status="IN_PROGRESS",
                body_hash=request_hash,
                response_status=None,
                response_body=None,
                created_at=_now(),
            )
        )
        rowcount = cast(CursorResult[Any], result).rowcount
        await session.commit()
        return Reserved() if rowcount == 1 else InProgress()

    async def complete(
        self,
        session: AsyncSession,
        scope: str,
        key: str,
        response_status: int,
        response_body: dict[str, Any],
    ) -> None:
        """Store the response ON THE CALLER'S SESSION — commits with the
        business write or not at all. No commit here, deliberately."""
        await session.execute(
            self._t.update()
            .where((self._t.c.scope == scope) & (self._t.c.idem_key == key))
            .values(
                status="COMPLETE",
                response_status=response_status,
                response_body=response_body,
            )
        )

    async def release(self, scope: str, key: str) -> None:
        """Free a key after a deterministic business failure — own short tx."""
        async with self._sessions() as session:
            await session.execute(
                self._t.delete().where((self._t.c.scope == scope) & (self._t.c.idem_key == key))
            )
            await session.commit()
