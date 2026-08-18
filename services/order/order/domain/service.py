"""Order domain — quote (S2), idempotent placement + reads (S3).

Placement is split at the seam ADR-0023 chose: this module does everything
that must answer the customer SYNCHRONOUSLY — reserve the idempotency key,
resolve the address, snapshot the menu, price the cart — and then hands a
fully-priced PlacementInput to the saga, which writes the order row inside
its own first activity and reports back.

So the four-things-at-once transaction (FR-14/16) still exists, unchanged;
it simply lives in the worker now (activities.create_order). What this
module keeps is the part with a customer waiting on it: every deterministic
refusal (PRICE_CHANGED, ITEM_UNAVAILABLE, RESTAURANT_CLOSED, unknown
address) is still raised here, in-process, with the key released for a
clean retry — no workflow round trip to learn the cart is stale.
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
from smartfood_otel import get_logger
from smartfood_pricing import Line, PricedOrder, PricingConfig, PricingError, price_order
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import OrderRepo, decode_cursor, encode_cursor
from ..db import OrderStatus
from ..values import LineSnapshot, PlacementInput
from .ports import (
    AddressNotFound,
    CatalogPort,
    IdentityPort,
    PlacementPending,
    SagaClosed,
    SagaGone,
    SagaPort,
    SagaUnavailable,
)

log = get_logger("order.service")


class OrderNotFound(Exception):
    pass


class InvalidCursor(Exception):
    pass


@dataclass(frozen=True)
class Placed:
    order_id: str
    status: str = "PLACED"


PlaceOutcome = Placed | PlacementPending | Replay | InProgress | BodyMismatch

# Placement's order id is DERIVED from the idempotency key, never random
# (ADR-0023). Two requests carrying the same key MUST land on the same id:
# once the order row is written by a worker rather than by this process,
# the window between reserving the key and completing it is long enough for
# the store's stale-IN_PROGRESS takeover to fire, and a fresh uuid4() there
# would mint a second id, a second ord:: workflow, and a second dinner.
# uuid5 makes that retry converge on the row that already exists instead.
_ORDER_NS = uuid.UUID("9f2c7b41-6d3e-4a58-8c0f-1e7b5a2d9c34")


def order_id_for(scope: str, idem_key: str) -> str:
    return f"ord_{uuid.uuid5(_ORDER_NS, f'{scope}:{idem_key}').hex}"


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


def placement_response(order_id: str, status: str = "PLACED") -> dict[str, str]:
    """THE 202 placement body. It is stored for idempotent replay (by the
    create_order ACTIVITY now) and returned on the fresh path — with two
    authors the replay could drift from the live shape; with one it cannot."""
    return {"order_id": order_id, "status": status}


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
        assert self._identity and self._saga and self._idempotency
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

        order_id = order_id_for(user_id, idem_key)
        now = _now()
        line_snapshots = [
            LineSnapshot(
                menu_item_id=line.item_id,
                name=line.name,
                unit_price_cents=line.unit_price_cents,
                qty=line.qty,
                line_total_cents=line.line_total_cents,
                options=[option.model_dump() for option in line.options],
            )
            for line in priced.lines
        ]
        pricing_snapshot = {**priced.totals.model_dump(), "currency": priced.currency}
        address_snapshot = {
            "address_id": address["id"],
            **{k: address[k] for k in ("label", "line1", "city", "lat", "lon")},
        }
        placement = PlacementInput(
            order_id=order_id,
            # The key travels with the work: the activity that writes the
            # order is the one that marks this key COMPLETE, in the same tx.
            scope=user_id,
            idem_key=idem_key,
            user_id=user_id,
            restaurant_id=restaurant_id,
            restaurant_name=priced.restaurant_name,
            card_token=card_token,
            menu_version=priced.menu_version,
            currency=priced.currency,
            amount_cents=priced.totals.total_cents,
            # Stamped HERE, not in the activity: a retried activity must
            # rewrite identical rows, and now() inside it would not.
            placed_at=now.isoformat(),
            lines=line_snapshots,
            pricing_snapshot=pricing_snapshot,
            address_snapshot=address_snapshot,
        )

        # From here the workflow owns the order. NOTE what is deliberately
        # absent: no release() on failure past this line. Once the start RPC
        # has been attempted we cannot know whether Temporal took it, and
        # freeing the key would let a retry mint a SECOND order id against a
        # workflow that may well be alive. Leaving the key IN_PROGRESS makes
        # the retry either wait (409) or take over onto the SAME id — both
        # safe (ADR-0023).
        try:
            ack = await self._saga.place(placement)
        except SagaClosed:
            # The derived id belongs to an order that already ran its course
            # (key reused past its 24h replay TTL). Returning that order is
            # the honest idempotent answer; a missing row would mean the id
            # collided with a workflow that never wrote one — an ops case.
            row = await self._order_row(order_id)
            if row is None:
                raise SagaUnavailable(f"closed saga with no order {order_id}") from None
            log.info("placement adopted a finished order", order_id=order_id, status=row.status)
            return Placed(order_id=order_id, status=str(row.status))
        if isinstance(ack, PlacementPending):
            log.warning(
                "placement pending — workflow durable, row not yet visible", order_id=order_id
            )
            return ack
        return Placed(order_id=ack.order_id, status=ack.status)

    async def _order_row(self, order_id: str) -> Row[Any] | None:
        assert self._sessions
        async with self._sessions() as session:
            return await OrderRepo(session).get_order_any(order_id)

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
            # with no reachable workflow is now genuinely anomalous (the row
            # exists BECAUSE a saga made it — ADR-0023), so the honest answer
            # is 503 "try again", not a cancellation we cannot deliver.
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
