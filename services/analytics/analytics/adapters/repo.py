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

from ..db import (
    assistant_facts,
    menu_views,
    order_facts,
    order_item_facts,
    restaurant_brands,
)


def _scoped(columns: Any, restaurant_id: str) -> sa.ColumnElement[bool]:
    """Owner scoping (ADR-0028): the claim is the BRAND id, rows carry both
    the branch and (once healed) the brand — either arm owns the row. The
    restaurant_id arm also keeps pre-repoint rows and old branch-scoped
    tokens visible through the transition window."""
    return (columns.restaurant_id == restaurant_id) | (columns.brand_id == restaurant_id)


def _within_hours(
    dialect: str,
    start: sa.ColumnElement[Any],
    moment: sa.ColumnElement[Any],
    hours: int,
) -> sa.ColumnElement[bool]:
    """`moment` lands in [start, start + hours) — the bounded attribution
    window every "did X follow Y?" read here needs.

    Per-dialect because the arithmetic is: PG adds an interval, sqlite
    subtracts julian days. `make_interval` rather than a built SQL string
    so `hours` travels as a bound parameter and never as text."""
    if dialect == "postgresql":  # pragma: no cover — PG-only
        window_end = start + sa.func.make_interval(0, 0, 0, 0, hours)
        return (moment >= start) & (moment < window_end)
    elapsed = sa.func.julianday(moment) - sa.func.julianday(start)
    return (elapsed >= 0) & (elapsed < hours / 24.0)


def _cited_any(dialect: str, ids: sa.ColumnElement[Any]) -> sa.ColumnElement[bool]:
    """The id list is non-empty — ARRAY on PG, JSON on sqlite (`_slugs`)."""
    if dialect == "postgresql":  # pragma: no cover — PG-only
        return sa.func.cardinality(ids) > 0
    return sa.func.json_array_length(ids) > 0


def _cites(
    dialect: str, ids: sa.ColumnElement[Any], item: sa.ColumnElement[Any]
) -> sa.ColumnElement[bool]:
    """`item` is one of the ids. Containment, not a join table: the ids are
    a derived read model that lives on the row being filtered."""
    if dialect == "postgresql":  # pragma: no cover — PG-only
        return item == sa.any_(ids)
    each = sa.func.json_each(ids).table_valued("value")
    # `correlate_except` is load-bearing, not tidiness. `item` belongs to a
    # table that may be TWO levels out, and auto-correlation only reaches
    # the immediately enclosing SELECT — so without this the table gets
    # re-added to this subquery's FROM and the predicate stops asking "did
    # THIS row match" and starts asking "did ANY row match", which is true
    # almost always. A cartesian product, and a silently inflated metric.
    inner = sa.select(sa.literal(1)).select_from(each).where(sa.column("value") == item)
    return sa.exists(inner.correlate_except(each))


def _people(column: Any) -> sa.ColumnElement[int]:
    """Distinct REAL customers, excluding the empty `user_id`.

    `assistant_acceptance` and `assistant_conversion` already excluded it
    from their joins; the counting queries did not, so every anonymous turn
    collapsed into one synthetic person who — sharing a `user_id` with every
    other anonymous turn — also looked like a RETURNING customer. Two
    strangers became one regular, and `returning_rate` was wrong in both
    numerator and denominator.
    """
    return sa.func.count(sa.distinct(sa.case((column != "", column))))


def _claim_cites(dialect: str, ids: sa.ColumnElement[Any], claim: str) -> sa.ColumnElement[bool]:
    """This owner's claim is among the ids the answer CITED.

    Two arms for the same reason `_scoped` has two: a citation names a
    BRANCH (that is what the index carries) and a restaurant admin's claim
    is normally the BRAND. The direct arm covers a claim that is itself a
    branch — an old token, or a restaurant with no brand — and the second
    asks the mapping which branches this brand owns. The namespaces are
    disjoint (ADR-0028), which is what makes OR-ing them safe.
    """
    rb = restaurant_brands.c
    owns_a_cited_branch = sa.exists(
        sa.select(sa.literal(1)).where(
            (rb.brand_id == claim) & _cites(dialect, ids, rb.restaurant_id)
        )
    )
    return _cites(dialect, ids, sa.literal(claim)) | owns_a_cited_branch


# Which columns each event type contributes beyond the always-updated base.
_EVENT_COLUMNS: dict[str, str] = {
    "OrderPlaced": "placed_at",
    "OrderConfirmed": "confirmed_at",
    "OrderDelivered": "delivered_at",
    "OrderCancelled": "cancelled_at",
    "OrderSettled": "settled_at",
}

_REJECTION_REASONS = ("restaurant_rejected", "restaurant_timeout")

# How long an action may follow a browse or an answer and still be credited
# to it. One constant for both funnels on purpose: "converted" has to mean
# the same span of time whether the nudge was a menu page or the assistant,
# or the two numbers sit on a dashboard inviting a comparison they do not
# support.
_ATTRIBUTION_HOURS = 24


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


def item_values(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One OrderPlaced → one row per distinct menu item on it (FR-96).

    Pure, like `event_values`, and for the same reason: the convergence
    guard lives here once, whether events land singly or in a batch.

    **Quantities are summed across lines sharing a menu item.** The cart
    splits a line per option combination, so "large, no chilli" and "small"
    are two lines and one dish. Keying on `(order_id, menu_item_id)` without
    summing would silently drop one of them; keying per line would make the
    row a line number, which is not a fact anybody wants to join on.

    A line with no `menu_item_id` is skipped rather than fatal: the
    recommender keys on item ids, so a row without one is unusable, and
    parking the whole topic over a malformed line would take the order facts
    down with it.
    """
    placed_at = datetime.fromisoformat(payload["placed_at"])
    merged: dict[str, dict[str, Any]] = {}
    for line in payload.get("items") or []:
        item_id = line.get("menu_item_id")
        if not item_id:
            continue
        row = merged.get(item_id)
        if row is None:
            merged[item_id] = {
                "order_id": payload["order_id"],
                "menu_item_id": item_id,
                "restaurant_id": payload["restaurant_id"],
                "brand_id": payload.get("brand_id"),
                "user_id": payload.get("user_id", ""),
                "name_snapshot": line.get("name", ""),
                "qty": int(line.get("qty", 0)),
                "line_total_cents": int(line.get("line_total_cents", 0)),
                "placed_at": placed_at,
            }
            continue
        row["qty"] += int(line.get("qty", 0))
        row["line_total_cents"] += int(line.get("line_total_cents", 0))
    return list(merged.values())


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

    async def insert_item_facts(self, rows: list[dict[str, Any]]) -> None:
        """Bulk item fold: one multi-VALUES statement, DO NOTHING on the
        composite key.

        DO NOTHING rather than DO UPDATE, and that is the whole redelivery
        story: an item fact is written once from OrderPlaced and never
        changes — the order's lifecycle lives on `order_facts`. A
        redelivered OrderPlaced therefore has nothing to update and must not
        pretend otherwise. It also makes duplicate keys WITHIN one statement
        legal, which a batch spanning a replayed partition routinely has.
        """
        if not rows:
            return
        insert = pg_insert if self._s.bind.dialect.name == "postgresql" else sqlite_insert
        await self._s.execute(
            insert(order_item_facts)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["order_id", "menu_item_id"])
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

    async def record_brand(self, restaurant_id: str, brand_id: str, at: datetime) -> None:
        """One branch → its brand, upserted. DO UPDATE and not DO NOTHING:
        a repoint MOVES a branch, and a mapping that kept the first answer
        would scope an owner's history to a brand that no longer owns it."""
        insert = pg_insert if self._s.bind.dialect.name == "postgresql" else sqlite_insert
        statement = insert(restaurant_brands).values(
            restaurant_id=restaurant_id, brand_id=brand_id, updated_at=at
        )
        await self._s.execute(
            statement.on_conflict_do_update(
                index_elements=["restaurant_id"],
                set_={
                    "brand_id": statement.excluded.brand_id,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

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
        in_window = _within_hours(
            self._s.bind.dialect.name, mv.viewed_at, f.placed_at, _ATTRIBUTION_HOURS
        )
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

    async def insert_assistant_facts(self, rows: list[dict[str, Any]]) -> None:
        """Bulk interaction fold, DO UPDATE on `message_id` (FR-94).

        DO UPDATE rather than DO NOTHING, which is the opposite call from
        `insert_item_facts` and for a reason worth stating: an item fact is
        written once from OrderPlaced and never changes, so a redelivery has
        nothing to say. An interaction fact CAN legitimately be restated —
        a turn that streamed and then failed at the last token settles
        twice, and the second fact is the true one. Both carry absolute
        values keyed by the message, so converging on the latest is correct
        and a counter would not be.

        Duplicate keys inside one batch are therefore possible and must not
        abort it: the last write of a `message_id` wins, which is also the
        order the partition delivered them in.
        """
        if not rows:
            return
        deduped: dict[str, dict[str, Any]] = {row["message_id"]: row for row in rows}
        insert = pg_insert if self._s.bind.dialect.name == "postgresql" else sqlite_insert
        statement = insert(assistant_facts).values(list(deduped.values()))
        await self._s.execute(
            statement.on_conflict_do_update(
                index_elements=["message_id"],
                set_={
                    column: statement.excluded[column]
                    for column in (
                        "outcome",
                        "refusal_reason",
                        "item_ids",
                        "restaurant_ids",
                        "candidates",
                        "ungrounded",
                        "duration_ms",
                        "occurred_at",
                    )
                },
            )
        )

    # ── the assistant's aggregates (FR-95) ─────────────────────────

    async def assistant_totals(self, since: datetime) -> dict[str, Any]:
        """Usage, response time and grounding in one pass over the window.

        Response time is averaged three ways because one way is a lie: a
        cache hit returns in milliseconds and a generated answer in seconds,
        so the blended mean describes neither experience and drifts with the
        hit rate rather than with the system getting faster.
        """
        a = assistant_facts.c
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("turns"),
                    _people(a.user_id).label("users"),
                    sa.func.count(sa.distinct(a.conversation_id)).label("conversations"),
                    sa.func.avg(a.duration_ms).label("avg_ms"),
                    sa.func.sum(a.candidates).label("candidates"),
                    sa.func.sum(a.ungrounded).label("ungrounded"),
                ).where(a.occurred_at >= since)
            )
        ).one()
        return {
            "turns": row.turns or 0,
            "users": row.users or 0,
            "conversations": row.conversations or 0,
            "avg_ms": row.avg_ms,
            "candidates": int(row.candidates or 0),
            "ungrounded": int(row.ungrounded or 0),
        }

    async def assistant_outcomes(self, since: datetime) -> dict[str, int]:
        """Turns per outcome, GROUP BY rather than a fixed set of counts.

        An outcome the graph starts emitting shows up here on its own.
        Hard-coding the ones we know today would make the breakdown quietly
        stop summing to the turn count the day a new one lands — and the
        gap would read as a drop in questions asked, not as a missing row.
        """
        a = assistant_facts.c
        rows = (
            await self._s.execute(
                sa.select(a.outcome, sa.func.count().label("turns"))
                .where(a.occurred_at >= since)
                .group_by(a.outcome)
                .order_by(sa.func.count().desc(), a.outcome)
            )
        ).all()
        return {row.outcome: row.turns for row in rows}

    async def assistant_returning(self, since: datetime) -> int:
        """Customers who came back: >1 distinct CONVERSATION in the window.

        Conversations, not turns. A long single conversation is one visit
        that went well; two conversations are two occasions the assistant
        was worth opening, which is the thing engagement is asking about.
        """
        a = assistant_facts.c
        per_user = (
            sa.select(a.user_id)
            .where((a.occurred_at >= since) & (a.user_id != ""))
            .group_by(a.user_id)
            .having(sa.func.count(sa.distinct(a.conversation_id)) > 1)
            .subquery()
        )
        count = (
            await self._s.execute(sa.select(sa.func.count()).select_from(per_user))
        ).scalar_one()
        return int(count or 0)

    async def assistant_acceptance(self, since: datetime) -> dict[str, int]:
        """Recommendation acceptance, measured PER TURN: of the turns that
        named at least one dish, how many were followed by that customer
        ordering one of those dishes inside the attribution window.

        Per turn and not per item, which is the choice worth defending. An
        answer naming five dishes is one recommendation a customer acts on
        or does not; scoring it per item caps it at 20% for behaving
        perfectly, so the rate would fall every time the answers got more
        helpful.

        The denominator excludes an empty `user_id`. There should not be
        one — the chat route is authenticated — but if one ever appears it
        would join to every anonymous order row in the table and credit the
        assistant with strangers' dinners. The same lesson `funnel` learned
        about anonymous viewers, applied before it can bite.
        """
        a, i = assistant_facts.c, order_item_facts.c
        dialect = self._s.bind.dialect.name
        # Cited, not retrieved: `item_ids` is what the ANSWER named. A dish
        # the customer was never shown cannot have been accepted.
        ordered = sa.exists(
            sa.select(sa.literal(1)).where(
                (i.user_id == a.user_id)
                & _cites(dialect, a.item_ids, i.menu_item_id)
                & _within_hours(dialect, a.occurred_at, i.placed_at, _ATTRIBUTION_HOURS)
            )
        )
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("recommending"),
                    sa.func.sum(sa.case((ordered, 1), else_=0)).label("accepted"),
                ).where(
                    (a.occurred_at >= since) & (a.user_id != "") & _cited_any(dialect, a.item_ids)
                )
            )
        ).one()
        return {
            "recommending": row.recommending or 0,
            "accepted": int(row.accepted or 0),
            "window_hours": _ATTRIBUTION_HOURS,
        }

    async def assistant_conversion(self, since: datetime) -> dict[str, int]:
        """Did an interaction lead to an order? (FR-97)

        Two numbers from two directions, because one number cannot answer
        both questions honestly:

        `converted` is counted TURN-side — of the turns that pointed a
        customer at a restaurant, how many were followed by that customer
        ordering there inside the window. That is a rate about answers.

        `orders` is counted ORDER-side, distinct. Three turns about the same
        restaurant followed by one dinner are three turns that worked and
        ONE order; counting the turn-side numerator as orders would report
        three, and the revenue beside it would be triple-counted. The two
        denominators differ on purpose and the windows do too: the turn-side
        window bounds `occurred_at`, the order-side one bounds `placed_at`,
        so an order here may be attributed to a turn just before `since`.

        Matching is on the branch id the answer CITED. Citations carry
        branch ids (`rst_…`) and order facts carry the branch, so they meet
        directly; if the recommender ever starts citing a BRAND this needs
        the `_scoped` disjunction, or the rate silently reads zero.

        A caveat no column can carry: this is correlation inside a window,
        not a controlled measurement. A customer who was going to order
        anyway and asked a question first lands in `converted` too. The
        number is worth having and is not worth calling causation.
        """
        a, f = assistant_facts.c, order_facts.c
        dialect = self._s.bind.dialect.name
        in_window = _within_hours(dialect, a.occurred_at, f.placed_at, _ATTRIBUTION_HOURS)
        same_customer_and_place = (
            (f.user_id == a.user_id)
            & _cites(dialect, a.restaurant_ids, f.restaurant_id)
            & in_window
        )
        turn_side = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("naming"),
                    sa.func.sum(
                        sa.case(
                            (sa.exists(sa.select(sa.literal(1)).where(same_customer_and_place)), 1),
                            else_=0,
                        )
                    ).label("converted"),
                ).where(
                    (a.occurred_at >= since)
                    # Same exclusion acceptance makes, for the same reason:
                    # an empty user_id would join to every anonymous order.
                    & (a.user_id != "")
                    & _cited_any(dialect, a.restaurant_ids)
                )
            )
        ).one()
        attributed = sa.exists(
            sa.select(sa.literal(1)).where(same_customer_and_place & (a.user_id != ""))
        )
        order_side = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("orders"),
                    # Revenue counts SETTLED only — the house rule the
                    # lifetime block already follows. An order the kitchen
                    # rejected still converted; it just never became money.
                    sa.func.sum(sa.case((f.settled_at.is_not(None), f.total_cents), else_=0)).label(
                        "revenue_cents"
                    ),
                ).where((f.placed_at >= since) & (f.user_id != "") & attributed)
            )
        ).one()
        return {
            "naming": turn_side.naming or 0,
            "converted": int(turn_side.converted or 0),
            "orders": order_side.orders or 0,
            "revenue_cents": int(order_side.revenue_cents or 0),
            "window_hours": _ATTRIBUTION_HOURS,
        }

    # ── one owner's AI insights (FR-98), scoped by the CLAIM ───────

    async def restaurant_assistant_views(self, claim: str, since: datetime) -> dict[str, int]:
        """Turns whose answer named this owner, and how many customers
        asked. The AI-driven equivalent of a menu view: the moment the
        assistant put this restaurant in front of someone."""
        a = assistant_facts.c
        row = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("turns"),
                    _people(a.user_id).label("customers"),
                ).where(
                    (a.occurred_at >= since)
                    & _claim_cites(self._s.bind.dialect.name, a.restaurant_ids, claim)
                )
            )
        ).one()
        return {"turns": row.turns or 0, "customers": row.customers or 0}

    async def restaurant_assistant_conversion(self, claim: str, since: datetime) -> dict[str, int]:
        """This owner's half of FR-97, counted the same two ways and scoped
        on BOTH ends: a turn that named this owner, followed by an order AT
        this owner. Neither side alone is this owner's business — a turn
        that mentioned them and sent the customer elsewhere is not their
        conversion, and an order they won without the assistant is not
        AI-driven."""
        a, f = assistant_facts.c, order_facts.c
        dialect = self._s.bind.dialect.name
        mine = _claim_cites(dialect, a.restaurant_ids, claim)
        # `_scoped(f, claim)` and NOT `_cites(a.restaurant_ids,
        # f.restaurant_id)`, which is the correction an adversarial pass
        # forced and it was wrong in both directions at once.
        #
        # Branch-exactness OVER-counted: `mine` already requires the turn to
        # have cited this owner, so matching the order against ANY cited
        # branch let a turn that named me and a competitor count as my
        # conversion when the customer ate at the competitor — other
        # tenants' orders moving my rate, and a reading channel on their
        # traffic. It also UNDER-counted: a brand whose assistant named one
        # branch lost every customer who ordered from a sibling branch.
        #
        # The owner-level rule the docstring states is the right one and it
        # fixes both: cited this owner, then ordered at this owner. The
        # platform-wide `assistant_conversion` keeps branch-exact matching,
        # where there is no claim and no owner to be level with.
        pair = (
            (f.user_id == a.user_id)
            & (a.user_id != "")
            & _scoped(f, claim)
            & _within_hours(dialect, a.occurred_at, f.placed_at, _ATTRIBUTION_HOURS)
        )
        turn_side = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("naming"),
                    sa.func.sum(
                        sa.case(
                            (sa.exists(sa.select(sa.literal(1)).where(pair)), 1),
                            else_=0,
                        )
                    ).label("converted"),
                ).where((a.occurred_at >= since) & (a.user_id != "") & mine)
            )
        ).one()
        order_side = (
            await self._s.execute(
                sa.select(
                    sa.func.count().label("orders"),
                    sa.func.sum(sa.case((f.settled_at.is_not(None), f.total_cents), else_=0)).label(
                        "revenue_cents"
                    ),
                ).where(
                    (f.placed_at >= since)
                    & (f.user_id != "")
                    & _scoped(f, claim)
                    & sa.exists(sa.select(sa.literal(1)).where(pair & mine))
                )
            )
        ).one()
        return {
            "naming": turn_side.naming or 0,
            "converted": int(turn_side.converted or 0),
            "orders": order_side.orders or 0,
            "revenue_cents": int(order_side.revenue_cents or 0),
            "window_hours": _ATTRIBUTION_HOURS,
        }
