"""Recording what was shown, and deriving what was accepted (FR-79).

The whole point of this module is that nothing here takes a client's word
for an acceptance. A showing is written when we show something; an
acceptance is computed when an order arrives and is found to contain a dish
we suggested. A surface that reported its own success would produce a number
that improves whenever the client changes, and no consumer downstream could
tell that from the product getting better.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import sqlalchemy as sa
from smartfood_kafka import EventType
from smartfood_outbox import stage_event as stage_outbox_event
from sqlalchemy import CursorResult
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import outbox, recommendation_acceptances, recommendations_shown


class AttributionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

    async def record_shown(
        self,
        *,
        user_id: str,
        city: str,
        surface: str,
        basis: str,
        item_ids: Sequence[str],
        now: datetime,
    ) -> str | None:
        """Write a showing and stage its event, in the caller's transaction.

        None when there was nothing to show: an empty recommendation is not
        a recommendation, and counting it would put a zero in the
        denominator of the acceptance rate for a moment when the customer
        was offered nothing to accept.
        """
        if not item_ids:
            return None
        shown_id = f"rec_{uuid4().hex}"
        await self._s.execute(
            recommendations_shown.insert().values(
                id=shown_id,
                user_id=user_id,
                city=city,
                surface=surface,
                basis=basis,
                item_ids=list(item_ids),
                shown_at=now,
            )
        )
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="recommendation",
            aggregate_id=shown_id,
            event_type=EventType.RECOMMENDATION_SHOWN,
            payload={
                "recommendation_id": shown_id,
                "user_id": user_id,
                "city": city,
                "surface": surface,
                "basis": basis,
                "item_ids": list(item_ids),
            },
            now=now,
        )
        return shown_id

    async def recent_showings(
        self, *, user_id: str, before: datetime, window: timedelta
    ) -> list[tuple[str, Sequence[str]]]:
        """`(shown_id, item_ids)` for what this customer was shown lately.

        Bounded BEFORE the order as well as after it: a showing that
        happened after the order was placed cannot have caused it, and on a
        replayed topic the clock is the only thing that says so.
        """
        rows = await self._s.execute(
            sa.select(recommendations_shown.c.id, recommendations_shown.c.item_ids).where(
                recommendations_shown.c.user_id == user_id,
                recommendations_shown.c.shown_at <= before,
                recommendations_shown.c.shown_at >= before - window,
            )
        )
        return [(row.id, list(row.item_ids or ())) for row in rows]

    async def record_acceptance(
        self,
        *,
        shown_id: str,
        order_id: str,
        item_ids: Sequence[str],
        accepted_at: datetime,
        now: datetime,
    ) -> bool:
        """Claim this (showing, order) pair. False if it was already claimed.

        The guard that makes a derived fact safe on an at-least-once topic:
        a redelivered `OrderPlaced` loses the insert and therefore never
        reaches the staging call, so one order accepts one showing exactly
        once (ADR-0035).
        """
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        result = cast(
            "CursorResult[Any]",
            await self._s.execute(
                insert(recommendation_acceptances)
                .values(
                    shown_id=shown_id,
                    order_id=order_id,
                    item_ids=list(item_ids),
                    accepted_at=accepted_at,
                )
                .on_conflict_do_nothing(index_elements=["shown_id", "order_id"])
            ),
        )
        if result.rowcount != 1:
            return False
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="recommendation",
            aggregate_id=shown_id,
            event_type=EventType.RECOMMENDATION_ACCEPTED,
            payload={
                "recommendation_id": shown_id,
                "order_id": order_id,
                # The intersection, not a boolean: an order that took one of
                # five suggestions is not the same signal as one that took
                # all five.
                "item_ids": list(item_ids),
            },
            now=now,
        )
        return True
