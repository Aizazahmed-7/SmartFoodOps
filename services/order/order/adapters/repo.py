"""All order SQL. The domain owns transactions; this owns statements.
Ownership lives in every WHERE clause (user_id scoping → 404, docs §5.2)."""

import base64
import json
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from smartfood_outbox import stage_event as stage_outbox_event
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import order_items, orders, outbox


def encode_cursor(placed_at: datetime, order_id: str) -> str:
    raw = json.dumps([placed_at.isoformat(), order_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Raises ValueError on ANY malformed cursor — the route maps to 422.
    Normalized: valid base64 of the wrong JSON shape raises TypeError
    variants internally, and a cursor bug must never surface as a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        placed_at_iso, order_id = json.loads(raw)
        return datetime.fromisoformat(placed_at_iso), str(order_id)
    except (TypeError, ValueError) as exc:  # binascii/JSON errors are ValueErrors
        raise ValueError("malformed cursor") from exc


class OrderRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def insert_order(
        self,
        *,
        order_id: str,
        user_id: str,
        restaurant_id: str,
        restaurant_name: str,
        card_token: str,
        menu_version: int,
        pricing_snapshot: dict[str, Any],
        address_snapshot: dict[str, Any],
        lines: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        await self._s.execute(
            orders.insert().values(
                order_id=order_id,
                user_id=user_id,
                restaurant_id=restaurant_id,
                restaurant_name_snapshot=restaurant_name,
                status="PLACED",
                aggregate_version=0,
                payment_method="CARD",
                card_token=card_token,
                menu_version=menu_version,
                pricing_snapshot=pricing_snapshot,
                delivery_address_snapshot=address_snapshot,
                placed_at=now,
                updated_at=now,
            )
        )
        await self._s.execute(
            order_items.insert(),
            [
                {
                    "order_id": order_id,
                    "line_no": line_no,
                    "menu_item_id": line["menu_item_id"],
                    "name_snapshot": line["name"],
                    "unit_price_cents": line["unit_price_cents"],
                    "qty": line["qty"],
                    "options_snapshot": line["options"],
                    "line_total_cents": line["line_total_cents"],
                }
                for line_no, line in enumerate(lines)
            ],
        )

    async def get_order(self, *, user_id: str, order_id: str) -> Row[Any] | None:
        result = await self._s.execute(
            sa.select(orders).where((orders.c.order_id == order_id) & (orders.c.user_id == user_id))
        )
        return result.one_or_none()

    async def get_order_any(self, order_id: str) -> Row[Any] | None:
        """Unscoped read for saga activities — the system operating on its
        own database, not a user request (no ownership clause by design)."""
        result = await self._s.execute(sa.select(orders).where(orders.c.order_id == order_id))
        return result.one_or_none()

    async def get_items(self, order_id: str) -> list[Row[Any]]:
        result = await self._s.execute(
            sa.select(order_items)
            .where(order_items.c.order_id == order_id)
            .order_by(order_items.c.line_no)
        )
        return list(result.all())

    async def list_feed(
        self,
        *,
        restaurant_id: str,
        status: str,
        limit: int,
        after: tuple[datetime, str] | None,
    ) -> list[Row[Any]]:
        """Kitchen queue down ix_orders_feed — OLDEST first (a kitchen is a
        FIFO; customer history is the newest-first mirror). `after` is the
        exclusive continuation point; limit+1 rows = has_more probe."""
        query = (
            sa.select(orders)
            .where((orders.c.restaurant_id == restaurant_id) & (orders.c.status == status))
            .order_by(orders.c.placed_at.asc(), orders.c.order_id.asc())
            .limit(limit + 1)
        )
        if after is not None:
            query = query.where(sa.tuple_(orders.c.placed_at, orders.c.order_id) > after)
        result = await self._s.execute(query)
        return list(result.all())

    async def get_items_for(self, order_ids: list[str]) -> dict[str, list[Row[Any]]]:
        """One query for a whole feed page's lines (never per-order N+1)."""
        if not order_ids:
            return {}
        result = await self._s.execute(
            sa.select(order_items)
            .where(order_items.c.order_id.in_(order_ids))
            .order_by(order_items.c.order_id, order_items.c.line_no)
        )
        grouped: dict[str, list[Row[Any]]] = {}
        for row in result.all():
            grouped.setdefault(row.order_id, []).append(row)
        return grouped

    async def stale_placed_order_ids(self, cutoff: datetime) -> list[str]:
        """Orders the sweeper owes a saga: still PLACED past the age
        threshold. Rides the partial index ix_orders_sweeper — the set is
        near-empty in a healthy system (PLACED is a transient state)."""
        result = await self._s.execute(
            sa.select(orders.c.order_id)
            .where((orders.c.status == "PLACED") & (orders.c.placed_at < cutoff))
            .order_by(orders.c.placed_at)
        )
        return list(result.scalars().all())

    async def list_orders(
        self,
        *,
        user_id: str,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> list[Row[Any]]:
        """Keyset page down ix_orders_history: newest first, `before` is the
        exclusive continuation point. limit+1 rows = has_more probe."""
        query = (
            sa.select(orders)
            .where(orders.c.user_id == user_id)
            .order_by(orders.c.placed_at.desc(), orders.c.order_id.desc())
            .limit(limit + 1)
        )
        if before is not None:
            # Row-value comparison: (placed_at, order_id) < (:p, :o) — the
            # id is the tiebreaker for orders placed in the same microsecond.
            query = query.where(sa.tuple_(orders.c.placed_at, orders.c.order_id) < before)
        result = await self._s.execute(query)
        return list(result.all())

    async def stage_event(
        self,
        *,
        order_id: str,
        version: int,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="order",
            aggregate_id=order_id,
            version=version,
            event_type=event_type,
            payload=payload,
            now=now,
        )
