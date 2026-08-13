"""Order domain — quote (S2), idempotent placement + reads (S3).

Placement is the four-things-at-once transaction (FR-14/16): order row +
line snapshots + OrderPlaced outbox row + the idempotency completion, all
committing together. The saga start runs AFTER the commit — never network
inside an open transaction; the commit→start gap is the accepted exposure
the W3 sweeper closes (the outbox event IS the durable to-do).

Repo-verified flag #2: the route prices synchronously (client gets
PRICE_CHANGED immediately); S5's price_order activity will LOAD this
snapshot rather than recompute — same numbers, by construction.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smartfood_idempotency import (
    BodyMismatch,
    IdempotencyStore,
    InProgress,
    Replay,
    Reserved,
)
from smartfood_kafka import EventType
from smartfood_pricing import Line, PricedOrder, PricingConfig, PricingError, price_order
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import OrderRepo, decode_cursor, encode_cursor
from ..db import OrderStatus
from .ports import AddressNotFound, CatalogPort, IdentityPort, SagaGone, SagaPort, SagaUnavailable


class OrderNotFound(Exception):
    pass


class InvalidCursor(Exception):
    pass


@dataclass(frozen=True)
class Placed:
    order_id: str


PlaceOutcome = Placed | Replay | InProgress | BodyMismatch


# ── customer cancellation (S7) ─────────────────────────────────────

# Cancellable until the courier holds the food (FR-21). The workflow's
# set-guarded TRY_BEGIN_CANCEL is the authoritative, race-safe referee;
# this set is the API's honest fast answer.
CANCELLABLE_STATES = frozenset(
    {"PLACED", "VALIDATED", "PAYMENT_CLEARED", "CONFIRMED", "ACCEPTED", "PREPARING", "READY"}
)
CANCELLED_FAMILY = frozenset({"CANCELLING", "CANCELLED", "REFUNDED"})


@dataclass(frozen=True)
class CancelSubmitted:
    """Signal handed to the saga — 202; the tracking screen is the truth."""

    status: OrderStatus


@dataclass(frozen=True)
class CancelAlreadyDone:
    """The order is already cancelled (or being cancelled) — the desired
    end state exists, whoever caused it: 200, never an error."""

    status: OrderStatus
    cancel_reason: str | None


@dataclass(frozen=True)
class NotCancellable:
    status: OrderStatus


CancelOutcome = CancelSubmitted | CancelAlreadyDone | NotCancellable


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class OrderService:
    def __init__(
        self,
        catalog: CatalogPort,
        *,
        pricing: PricingConfig,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        identity: IdentityPort | None = None,
        saga: SagaPort | None = None,
        idempotency: IdempotencyStore | None = None,
    ):
        self._catalog = catalog
        self._pricing = pricing
        self._sessions = sessions
        self._identity = identity
        self._saga = saga
        self._idempotency = idempotency

    # ── quote (S2) ─────────────────────────────────────────────────

    async def quote(self, restaurant_id: str, lines: list[Line]) -> PricedOrder:
        """Price a cart against the CURRENT menu (expected_menu_version=None:
        a quote self-heals across menu edits — the response carries the
        version the client should re-pin its cart to)."""
        snapshot = await self._snapshot(restaurant_id, lines)
        return price_order(snapshot, lines, config=self._pricing)

    # ── placement (S3) ─────────────────────────────────────────────

    async def place(
        self,
        *,
        user_id: str,
        idem_key: str,
        request_hash: str,
        restaurant_id: str,
        menu_version: int,
        lines: list[Line],
        address_id: str,
        card_token: str,
    ) -> PlaceOutcome:
        assert self._sessions and self._identity and self._saga and self._idempotency
        outcome = await self._idempotency.reserve(user_id, idem_key, request_hash)
        if not isinstance(outcome, Reserved):
            return outcome

        try:
            # Server-side resolution only — the request carried IDs, never
            # address content or prices (api-standards §3).
            address = await self._identity.get_address(user_id, address_id)
            snapshot = await self._snapshot(restaurant_id, lines)
            priced = price_order(
                snapshot, lines, expected_menu_version=menu_version, config=self._pricing
            )
        except (PricingError, AddressNotFound):
            # Deterministic business refusal: free the key immediately — the
            # client will re-confirm with a fresh body and a fresh key.
            await self._idempotency.release(user_id, idem_key)
            raise

        order_id = f"ord_{uuid.uuid4().hex}"
        now = _now()
        response_body = {"order_id": order_id, "status": "PLACED"}
        line_dicts = [
            {
                "menu_item_id": line.item_id,
                "name": line.name,
                "unit_price_cents": line.unit_price_cents,
                "qty": line.qty,
                "options": [option.model_dump() for option in line.options],
                "line_total_cents": line.line_total_cents,
            }
            for line in priced.lines
        ]
        pricing_snapshot = {**priced.totals.model_dump(), "currency": priced.currency}
        address_snapshot = {
            "address_id": address["id"],
            **{k: address[k] for k in ("label", "line1", "city", "lat", "lon")},
        }

        async with self._sessions() as session:
            repo = OrderRepo(session)
            await repo.insert_order(
                order_id=order_id,
                user_id=user_id,
                restaurant_id=restaurant_id,
                restaurant_name=priced.restaurant_name,
                card_token=card_token,
                menu_version=priced.menu_version,
                pricing_snapshot=pricing_snapshot,
                address_snapshot=address_snapshot,
                lines=line_dicts,
                now=now,
            )
            await repo.stage_event(
                order_id=order_id,
                version=0,
                event_type=EventType.ORDER_PLACED,
                payload={
                    "order_id": order_id,
                    "user_id": user_id,
                    "restaurant_id": restaurant_id,
                    "restaurant_name": priced.restaurant_name,
                    "status": "PLACED",
                    "menu_version": priced.menu_version,
                    "items": line_dicts,
                    "totals": pricing_snapshot,
                    "delivery_address": address_snapshot,
                    "placed_at": now.isoformat(),
                },
                now=now,
            )
            # The stored response commits WITH the order — replay can never
            # disagree with reality, and a crash here re-executes cleanly.
            await self._idempotency.complete(session, user_id, idem_key, 202, response_body)
            await session.commit()

        await self._saga.start(order_id)  # after commit; stub until S5
        return Placed(order_id=order_id)

    # ── customer cancellation (S7) ─────────────────────────────────

    async def request_cancel(self, user_id: str, order_id: str) -> CancelOutcome:
        """Ownership-scoped read → classify → signal. The 202 is honest-async
        like the kitchen's decisions: 'submitted', not 'done' — the workflow
        referees the customer-vs-courier race against the DB row."""
        assert self._sessions and self._saga
        async with self._sessions() as session:
            row = await OrderRepo(session).get_order(user_id=user_id, order_id=order_id)
        if row is None:
            raise OrderNotFound  # not-found and not-yours are the same 404
        if row.status in CANCELLED_FAMILY:
            return CancelAlreadyDone(row.status, row.cancel_reason)
        if row.status not in CANCELLABLE_STATES:
            return NotCancellable(row.status)  # the courier already has it

        try:
            await self._saga.signal_cancel(order_id)
        except SagaGone:
            # The workflow finished between our read and the signal —
            # re-read and answer from the truth. A still-cancellable status
            # with NO workflow means the placement→start gap: the only sane
            # advice is retry (the sweeper story), so surface 503.
            async with self._sessions() as session:
                now = await OrderRepo(session).get_order(user_id=user_id, order_id=order_id)
            assert now is not None  # orders are never deleted
            if now.status in CANCELLED_FAMILY:
                return CancelAlreadyDone(now.status, now.cancel_reason)
            if now.status not in CANCELLABLE_STATES:
                return NotCancellable(now.status)
            raise SagaUnavailable("order workflow not yet running") from None
        return CancelSubmitted(row.status)

    # ── reads (S3) ─────────────────────────────────────────────────

    async def get_order(self, user_id: str, order_id: str) -> dict[str, Any]:
        assert self._sessions
        async with self._sessions() as session:
            repo = OrderRepo(session)
            row = await repo.get_order(user_id=user_id, order_id=order_id)
            if row is None:
                raise OrderNotFound
            items = await repo.get_items(order_id)
        snapshot = dict(row.pricing_snapshot)
        currency = snapshot.pop("currency")
        return {
            "order_id": row.order_id,
            "status": row.status,
            "restaurant_id": row.restaurant_id,
            "restaurant_name": row.restaurant_name_snapshot,
            "menu_version": row.menu_version,
            "placed_at": _aware(row.placed_at).isoformat(),
            "cancel_reason": row.cancel_reason,
            "currency": currency,
            "totals": snapshot,
            "delivery_address": row.delivery_address_snapshot,
            "items": [
                {
                    "menu_item_id": item.menu_item_id,
                    "name": item.name_snapshot,
                    "qty": item.qty,
                    "unit_price_cents": item.unit_price_cents,
                    "options": item.options_snapshot,
                    "line_total_cents": item.line_total_cents,
                }
                for item in items
            ],
        }

    async def list_orders(self, user_id: str, *, limit: int, cursor: str | None) -> dict[str, Any]:
        assert self._sessions
        before: tuple[datetime, str] | None = None
        if cursor is not None:
            try:
                before = decode_cursor(cursor)
            except (ValueError, TypeError):
                raise InvalidCursor from None
        async with self._sessions() as session:
            rows = await OrderRepo(session).list_orders(user_id=user_id, limit=limit, before=before)
        page, has_more = rows[:limit], len(rows) > limit
        next_cursor = (
            encode_cursor(_aware(page[-1].placed_at), page[-1].order_id) if has_more else None
        )
        return {
            "items": [self._summary(row) for row in page],
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _summary(row: Row[Any]) -> dict[str, Any]:
        return {
            "order_id": row.order_id,
            "restaurant_name": row.restaurant_name_snapshot,
            "status": row.status,
            "total_cents": row.pricing_snapshot["total_cents"],
            "placed_at": _aware(row.placed_at).isoformat(),
        }

    # ── helpers ────────────────────────────────────────────────────

    async def _snapshot(self, restaurant_id: str, lines: list[Line]) -> dict[str, Any]:
        # Dedupe ids, preserving order (two lines of the same item with
        # different options are one snapshot item).
        item_ids: list[str] = []
        for line in lines:
            if line.item_id not in item_ids:
                item_ids.append(line.item_id)
        return await self._catalog.get_snapshot(restaurant_id, item_ids)
