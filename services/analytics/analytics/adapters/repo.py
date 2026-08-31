"""SQL for the projector and the aggregate reads.

Upserts use the pg/sqlite dialect-split insert (the notification idiom):
ON CONFLICT DO UPDATE with only the columns THIS event owns, so events
compose — OrderDelivered filling delivered_at can never blank the
cancel_reason a racing... (there is no race: the topic key serializes a
single order's events; the narrow update set is still right, because it
makes redelivery idempotent column-by-column).

Duration math is per-dialect on purpose (epoch diff on PG, julianday on
sqlite): pulling rows to average in Python would cap or stream — both
wrong at volume. The database is good at this; let it.
"""

from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import menu_views, order_facts


def _scoped(columns: Any, restaurant_id: str) -> sa.ColumnElement[bool]:
    """Owner scoping (ADR-0028): the claim is the BRAND id, rows carry both
    the branch and (once healed) the brand — either arm owns the row. The
    restaurant_id arm also keeps pre-repoint rows and old branch-scoped
    tokens visible through the transition window."""
    return (columns.restaurant_id == restaurant_id) | (columns.brand_id == restaurant_id)


# Which columns each event type contributes beyond the always-updated base.
_EVENT_COLUMNS: dict[str, str] = {
    "OrderPlaced": "placed_at",
    "OrderConfirmed": "confirmed_at",
    "OrderDelivered": "delivered_at",
    "OrderCancelled": "cancelled_at",
    "OrderSettled": "settled_at",
}

_REJECTION_REASONS = ("restaurant_rejected", "restaurant_timeout")


def _total_cents(payload: dict[str, Any]) -> int:
    """The order total, defensively: `totals` is the stored pricing
    snapshot (a PricedOrder dump, which nests its own `totals`)."""
    totals = payload.get("totals") or {}
    if isinstance(totals.get("totals"), dict):
        totals = totals["totals"]
    value = totals.get("total_cents", 0)
    return int(value) if isinstance(value, (int, float)) else 0


def event_values(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """One lifecycle event → the fact-row columns it owns (None = unknown
    type, skipped for forward compatibility: a newer producer must not park
    this consumer's batches). Pure on purpose — the consumer's fold merges
    these dicts in batch order, and every convergence guard lives HERE,
    once, whether events land singly or bulk."""
    milestone = _EVENT_COLUMNS.get(event_type)
    if milestone is None:
        return None
    # Two producer shapes share this topic: transition events stamp
    # `occurred_at`; the OrderPlaced staged by create_order stamps
    # `placed_at` (the API's own clock — semantically the right moment
    # for that milestone anyway). History is immutable, so the READER
    # tolerates both; a payload with neither raises → retries → parks
    # with forensics, which is the right fate for a shapeless one.
    raw_ts = payload.get("occurred_at") or payload["placed_at"]
    occurred = datetime.fromisoformat(raw_ts)
    values: dict[str, Any] = {
        "order_id": payload["order_id"],
        "restaurant_id": payload["restaurant_id"],
        "user_id": payload.get("user_id", ""),
        "status": payload["status"],
        "aggregate_version": int(payload.get("aggregate_version", 0)),
        "total_cents": _total_cents(payload),
        "updated_at": occurred,
        milestone: occurred,
    }
    if event_type == "OrderCancelled":
        values["cancel_reason"] = payload.get("cancel_reason")
    # Only a KNOWN courier updates the column: pre-assignment events
    # carry null, and an out-of-order early event must never blank a
    # later stamp (the delivered_at convergence rule, applied again).
    if payload.get("rider_id"):
        values["rider_id"] = payload["rider_id"]
    # Same convergence rule as rider_id: only a KNOWN brand writes the
    # column — a legacy event replay must never blank a healed stamp.
    if payload.get("brand_id"):
        values["brand_id"] = payload["brand_id"]
    return values


def view_values(payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    """One MenuViewed → a menu_views row, keyed by the deterministic
    event id so redelivery lands on the PK and vanishes."""
    return {
        "view_id": event_id,
        "restaurant_id": payload["restaurant_id"],
        "brand_id": payload.get("brand_id"),
        "user_id": payload.get("user_id"),
        "viewed_at": datetime.fromisoformat(payload["viewed_at"]),
    }


class AnalyticsRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def upsert_facts(self, rows: list[dict[str, Any]]) -> None:
        """Bulk fold: rows arrive PRE-MERGED, one per order (the consumer's
        fold guarantees it — Postgres refuses one upsert statement touching
        the same row twice; sqlite tolerates it, so the test that guards
        the invariant lives on the FOLD, not here). Rows carry per-event
        column SETS ("only the columns this event owns" — the redelivery
        idempotency rule), so they group by signature: one multi-VALUES
        statement per distinct column set, `excluded` carrying each row's
        own values."""
        insert = pg_insert if self._s.bind.dialect.name == "postgresql" else sqlite_insert
        by_signature: dict[frozenset[str], list[dict[str, Any]]] = {}
        for row in rows:
            by_signature.setdefault(frozenset(row), []).append(row)
        for signature, group in by_signature.items():
            stmt = insert(order_facts).values(group)
            update_cols = {k: stmt.excluded[k] for k in signature if k != "order_id"}
            await self._s.execute(
                stmt.on_conflict_do_update(index_elements=["order_id"], set_=update_cols)
            )

    async def insert_views(self, rows: list[dict[str, Any]]) -> None:
        """Bulk MenuViewed fold: INSERT .. DO NOTHING on the deterministic
        view_id — one multi-VALUES statement per batch. Duplicate view_ids
        WITHIN the statement are legal for DO NOTHING (unlike DO UPDATE):
        redelivered or double-polled ids just skip."""
        if not rows:
            return
        insert = pg_insert if self._s.bind.dialect.name == "postgresql" else sqlite_insert
        await self._s.execute(
            insert(menu_views).values(rows).on_conflict_do_nothing(index_elements=["view_id"])
        )

    async def repoint_brand(self, restaurant_id: str, brand_id: str) -> int:
        """Backfill NULL brand_id for one branch's rows (facts and views) —
        the IS NULL predicate makes replay a no-op. Returns rows healed."""
        healed = 0
        for table in (order_facts, menu_views):
            result = await self._s.execute(
                table.update()
                .where((table.c.restaurant_id == restaurant_id) & (table.c.brand_id.is_(None)))
                .values(brand_id=brand_id)
            )
            healed += int(cast("CursorResult[Any]", result).rowcount)
        return healed

    # ── aggregate reads (all bounded by a `since` window) ──────────

    async def counts(self, since: datetime) -> dict[str, int]:
        c = order_facts.c
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("placed"),
                    sa.func.count(c.confirmed_at).label("confirmed"),
                    sa.func.count(c.delivered_at).label("delivered"),
                    sa.func.count(c.cancelled_at).label("cancelled"),
                    sa.func.sum(
                        sa.case((c.cancel_reason.in_(_REJECTION_REASONS), 1), else_=0)
                    ).label("rejected"),
                    sa.func.count(c.settled_at).label("settled"),
                    # Revenue = SETTLED only. An authorized hold is not income;
                    # counting CONFIRMED totals would book money that a cancel
                    # can still void.
                    sa.func.sum(sa.case((c.settled_at.is_not(None), c.total_cents), else_=0)).label(
                        "revenue_cents"
                    ),
                ).where(c.placed_at >= since)
            )
        ).one()
        return {
            "placed": row.placed or 0,
            "confirmed": row.confirmed or 0,
            "delivered": row.delivered or 0,
            "cancelled": row.cancelled or 0,
            "rejected": int(row.rejected or 0),
            "settled": row.settled or 0,
            "revenue_cents": int(row.revenue_cents or 0),
        }

    async def orders_per_restaurant(self, since: datetime, limit: int) -> list[dict[str, Any]]:
        c = order_facts.c
        rows = (
            await self._s.execute(
                sa.select(c.restaurant_id, sa.func.count().label("orders"))
                .where(c.placed_at >= since)
                .group_by(c.restaurant_id)
                .order_by(sa.desc("orders"), c.restaurant_id)
                .limit(limit)
            )
        ).all()
        return [{"restaurant_id": r.restaurant_id, "orders": r.orders} for r in rows]

    async def peak_hour(self, since: datetime) -> dict[str, int] | None:
        c = order_facts.c
        hour = sa.cast(sa.extract("hour", c.placed_at), sa.Integer).label("hour")
        row = (
            await self._s.execute(
                sa.select(hour, sa.func.count().label("orders"))
                .where(c.placed_at >= since)
                .group_by(hour)
                .order_by(sa.desc("orders"), hour)
                .limit(1)
            )
        ).one_or_none()
        return None if row is None else {"hour": int(row.hour), "orders": row.orders}

    async def avg_delivery_seconds(self, since: datetime) -> float | None:
        c = order_facts.c
        if self._s.bind.dialect.name == "postgresql":  # pragma: no cover — PG-only math,
            # exercised by the compose stack; the sqlite branch below is the unit-suite twin.
            seconds = sa.func.avg(sa.func.extract("epoch", c.delivered_at - c.placed_at))
        else:
            seconds = sa.func.avg(
                (sa.func.julianday(c.delivered_at) - sa.func.julianday(c.placed_at)) * 86400.0
            )
        value = (
            await self._s.execute(
                sa.select(seconds).where(c.delivered_at.is_not(None) & (c.placed_at >= since))
            )
        ).scalar_one_or_none()
        return None if value is None else float(value)

    async def daily(self, restaurant_id: str, since: datetime) -> list[dict[str, Any]]:
        """Per-day rollup for ONE restaurant — ownership lives in this
        WHERE clause. Computed from facts at read time; see db.py for why
        this is not an incrementally-maintained table."""
        c = order_facts.c
        day = sa.func.date(c.placed_at).label("day")
        rows = (
            await self._s.execute(
                sa.select(
                    day,
                    sa.func.count().label("orders"),
                    sa.func.count(c.cancelled_at).label("cancelled"),
                    sa.func.count(c.delivered_at).label("delivered"),
                    sa.func.sum(sa.case((c.settled_at.is_not(None), c.total_cents), else_=0)).label(
                        "revenue_cents"
                    ),
                )
                .where(_scoped(c, restaurant_id) & (c.placed_at >= since))
                .group_by(day)
                .order_by(day)
            )
        ).all()
        return [
            {
                "day": str(r.day),
                "orders": r.orders,
                "cancelled": r.cancelled,
                "delivered": r.delivered,
                "revenue_cents": int(r.revenue_cents or 0),
            }
            for r in rows
        ]

    async def restaurant_lifetime(self, restaurant_id: str) -> dict[str, int]:
        """All-time totals — deliberately a SEPARATE query with no window:
        folding lifetime numbers into the windowed one would make the
        window picker lie about one or the other."""
        c = order_facts.c
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("orders"),
                    sa.func.count(c.settled_at).label("settled"),
                    sa.func.count(c.cancelled_at).label("cancelled"),
                    sa.func.sum(sa.case((c.settled_at.is_not(None), c.total_cents), else_=0)).label(
                        "revenue_cents"
                    ),
                    sa.func.count(sa.distinct(c.user_id)).label("customers"),
                ).where(_scoped(c, restaurant_id))
            )
        ).one()
        repeat = (
            await self._s.execute(
                sa.select(sa.func.count()).select_from(
                    sa.select(c.user_id)
                    .where(_scoped(c, restaurant_id))
                    .group_by(c.user_id)
                    .having(sa.func.count() >= 2)
                    .subquery()
                )
            )
        ).scalar_one()
        return {
            "orders": row.orders or 0,
            "settled": row.settled or 0,
            "cancelled": row.cancelled or 0,
            "revenue_cents": int(row.revenue_cents or 0),
            "customers": row.customers or 0,
            "repeat_customers": int(repeat or 0),
        }

    async def funnel(self, restaurant_id: str, since: datetime) -> dict[str, int]:
        """Browse → order conversion, computed at read time. A viewer
        CONVERTED if they placed an order at this restaurant within 24h of
        a view. Conversion is measured over SIGNED-IN viewers only —
        anonymous views count toward volume, nothing else."""
        mv, f = menu_views.c, order_facts.c
        totals = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("views"),
                    sa.func.count(sa.distinct(mv.user_id)).label("viewers"),
                ).where(_scoped(mv, restaurant_id) & (mv.viewed_at >= since))
            )
        ).one()
        if self._s.bind.dialect.name == "postgresql":  # pragma: no cover — PG-only
            # interval math; the sqlite branch below is the unit-suite twin.
            window_end = mv.viewed_at + sa.text("interval '24 hours'")
            in_window = (f.placed_at >= mv.viewed_at) & (f.placed_at < window_end)
        else:
            day = 1.0
            in_window = (
                (sa.func.julianday(f.placed_at) - sa.func.julianday(mv.viewed_at)) >= 0
            ) & ((sa.func.julianday(f.placed_at) - sa.func.julianday(mv.viewed_at)) < day)
        converted = (
            await self._s.execute(
                sa.select(sa.func.count(sa.distinct(mv.user_id))).where(
                    _scoped(mv, restaurant_id)
                    & (mv.viewed_at >= since)
                    & mv.user_id.is_not(None)
                    & sa.exists(
                        sa.select(sa.literal(1)).where(
                            (f.user_id == mv.user_id)
                            & (f.restaurant_id == mv.restaurant_id)
                            & in_window
                        )
                    )
                )
            )
        ).scalar_one()
        return {
            "views": totals.views or 0,
            "viewers": totals.viewers or 0,
            "converted_viewers": int(converted or 0),
        }

    async def restaurant_counts(self, restaurant_id: str, since: datetime) -> dict[str, int]:
        c = order_facts.c
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("placed"),
                    sa.func.count(c.confirmed_at).label("confirmed"),
                    sa.func.count(c.cancelled_at).label("cancelled"),
                    sa.func.sum(
                        sa.case((c.cancel_reason.in_(_REJECTION_REASONS), 1), else_=0)
                    ).label("rejected"),
                    sa.func.count(c.settled_at).label("settled"),
                ).where(_scoped(c, restaurant_id) & (c.placed_at >= since))
            )
        ).one()
        return {
            "placed": row.placed or 0,
            "confirmed": row.confirmed or 0,
            "cancelled": row.cancelled or 0,
            "rejected": int(row.rejected or 0),
            "settled": row.settled or 0,
        }
