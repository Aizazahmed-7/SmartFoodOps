"""assistant_db writes that are not the vector index itself: the debounce
queue and the pointer that says which vector space is live.

The `VectorStore` adapter lives beside this file rather than inside it —
one is a queue, one is an index, and they fail for different reasons.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import knowledge_pending


@dataclass(frozen=True)
class Pending:
    """One restaurant's queued re-index, as the drain sees it."""

    restaurant_id: str
    payload: dict[str, Any]
    first_seen_at: datetime


class PendingRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    async def stage(
        self,
        *,
        restaurant_id: str,
        payload: dict[str, Any],
        now: datetime,
        debounce_s: float,
    ) -> None:
        """Queue one restaurant for re-indexing, no sooner than `debounce_s`
        from its FIRST unprocessed change.

        The conflict clause is the whole debounce, and it is deliberately
        asymmetric:

        - `payload` is overwritten, because catalog's events are full-state
          snapshots and the newest one supersedes every earlier one
          completely. There is no merge to do and no history to keep. It is
          also what `complete` guards on, so overwriting it is what makes a
          mid-drain edit survive.
        - `due_at` keeps the EARLIER of the two. That makes this a fixed
          window rather than a sliding one: an owner editing twenty dishes
          over five minutes is indexed once, `debounce_s` after the first
          edit, instead of never — a trailing debounce restarts on every
          event and can starve past NFR-28's 60 s freshness budget.
        - `first_seen_at` is not touched at all, so "this restaurant has
          been waiting four minutes" stays answerable.

        Written as ONE upsert rather than a read-then-write because two
        partitions of the same topic, or a redelivery racing a drain, would
        otherwise interleave into a lost update — and the symptom would be a
        restaurant that silently never re-indexes.

        `sa.case` rather than `LEAST`: Postgres has `LEAST`, sqlite spells
        the same thing `MIN`, and the unit suite runs on sqlite. A CASE
        compiles identically on both.
        """
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        due_at = now + timedelta(seconds=debounce_s)
        stmt = insert(knowledge_pending).values(
            restaurant_id=restaurant_id,
            payload=payload,
            due_at=due_at,
            first_seen_at=now,
        )
        await self._s.execute(
            stmt.on_conflict_do_update(
                index_elements=[knowledge_pending.c.restaurant_id],
                set_={
                    "payload": stmt.excluded.payload,
                    "due_at": sa.case(
                        (
                            knowledge_pending.c.due_at < stmt.excluded.due_at,
                            knowledge_pending.c.due_at,
                        ),
                        else_=stmt.excluded.due_at,
                    ),
                },
            )
        )

    async def due(self, *, now: datetime, limit: int) -> list[Pending]:
        """Restaurants whose window has closed, oldest deadline first.

        Oldest-first so a backlog drains in the order it accumulated: under
        load the restaurant that has been stale longest is the one whose
        customers are seeing the wrong menu.
        """
        rows = await self._s.execute(
            sa.select(knowledge_pending)
            .where(knowledge_pending.c.due_at <= now)
            .order_by(knowledge_pending.c.due_at)
            .limit(limit)
        )
        return [
            Pending(
                restaurant_id=row.restaurant_id,
                payload=row.payload,
                first_seen_at=row.first_seen_at,
            )
            for row in rows
        ]

    async def complete(self, *, restaurant_id: str, payload: dict[str, Any]) -> bool:
        """Remove a drained row — but ONLY if it still holds the payload we
        drained. Returns whether it did.

        The drain reads a row, then spends seconds embedding with no
        transaction open. An unguarded delete would silently discard any
        edit that landed in that gap, and nothing would report it: the index
        would simply stay wrong until the restaurant happened to change
        again. Compared against the PAYLOAD itself, a mid-flight edit leaves
        the row in place and the next tick picks it up.

        A redelivery of the SAME payload compares equal and is therefore
        correctly treated as already done. The comparison is sound on both
        dialects: JSONB equality is semantic, so a payload whose keys
        Postgres re-ordered on write still matches, and sqlite's JSON
        round-trips deterministically.
        """
        result = await self._s.execute(
            sa.delete(knowledge_pending).where(
                knowledge_pending.c.restaurant_id == restaurant_id,
                knowledge_pending.c.payload == payload,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)
