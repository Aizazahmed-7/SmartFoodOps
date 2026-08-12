"""Payment domain — the money operations behind the saga's activities.

Every operation is read-before-execute (FR-22) via the shared idempotency
store on payment's OWN table: scope "money", keys "{order_id}:{op}". The
gateway call uses the SAME string as its PSP idempotency key, so our
replay and the PSP's replay are aligned — a retry after any ambiguity
converges instead of double-charging.

Amounts NEVER arrive from the caller for capture/refund — they are read
from the stored authorization row (the caller proved the amount once, at
authorize; after that the row is the truth).

Decline is an outcome, not an error: it is stored and replayed with the
same 402 body forever — "was my card charged?" must have one answer.
"""

import hashlib
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import PaymentRepo
from .ports import PaymentGatewayPort, PspStateConflict, PspUnavailable

MONEY_SCOPE = "money"


@dataclass(frozen=True)
class PaymentOutcome:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


class MoneyOpInProgress(Exception):
    pass


class MoneyKeyMismatch(Exception):
    """Same money key, different parameters — a caller bug (422)."""


class PaymentStateConflict(Exception):
    """The operation doesn't fit the payment's state (capture w/o auth…)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _op_hash(*parts: object) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


class PaymentService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gateway: PaymentGatewayPort,
        store: IdempotencyStore,
    ):
        self._sessions = sessions
        self._gateway = gateway
        self._store = store

    # ── authorize ──────────────────────────────────────────────────

    async def authorize(
        self, order_id: str, *, amount_cents: int, currency: str, card_token: str
    ) -> PaymentOutcome:
        key = f"{order_id}:auth"
        reserved = await self._reserve(key, _op_hash("auth", order_id, amount_cents, currency))
        if reserved is not None:
            return reserved

        try:
            result = await self._gateway.authorize(
                key=key, amount_cents=amount_cents, currency=currency, card_token=card_token
            )
        except PspUnavailable:
            # Outcome unknown at the PSP — free OUR key so the retry
            # re-executes; the PSP's replay of the SAME key resolves it.
            await self._store.release(MONEY_SCOPE, key)
            raise

        if result.approved:
            status, body = (
                200,
                {
                    "order_id": order_id,
                    "status": "AUTHORIZED",
                    "amount_cents": amount_cents,
                    "payment_intent_id": result.psp_ref,
                },
            )
            row_status = "AUTHORIZED"
        else:
            status, body = (
                402,
                {"error": {"code": "PAYMENT_DECLINED", "message": "card declined"}},
            )
            row_status = "DECLINED"

        now = _now()
        async with self._sessions() as session:
            repo = PaymentRepo(session)
            try:
                await repo.insert_payment(
                    order_id=order_id,
                    status=row_status,
                    amount_cents=amount_cents,
                    currency=currency,
                    card_token=card_token,
                    payment_intent_id=result.psp_ref if result.approved else None,
                    now=now,
                )
            except IntegrityError:
                # A row already exists (expired-key re-execution long after
                # a committed attempt): converge on the stored truth.
                await session.rollback()
                existing = await repo.get_payment(order_id)
                assert existing is not None
                status, body = self._describe(existing)
                await self._store.complete(session, MONEY_SCOPE, key, status, body)
                await session.commit()
                return PaymentOutcome(status, body)
            if result.approved:
                await repo.stage_event(
                    order_id=order_id,
                    version=0,
                    event_type="PaymentAuthorized",
                    payload={
                        "order_id": order_id,
                        "status": "AUTHORIZED",
                        "amount_cents": amount_cents,
                        "currency": currency,
                        "payment_intent_id": result.psp_ref,
                    },
                    now=now,
                )
            await self._store.complete(session, MONEY_SCOPE, key, status, body)
            await session.commit()
        return PaymentOutcome(status, body)

    # ── capture / void / refund ────────────────────────────────────

    async def capture(self, order_id: str) -> PaymentOutcome:
        return await self._lifecycle(
            order_id,
            op="capture",
            expected="AUTHORIZED",
            target="CAPTURED",
            gateway_call=self._gateway.capture,
            ledger=("customer", "platform_cash"),  # money moves TO us
            event_type="PaymentCaptured",
        )

    async def void(self, order_id: str) -> PaymentOutcome:
        return await self._lifecycle(
            order_id,
            op="void",
            expected="AUTHORIZED",
            target="VOIDED",
            gateway_call=self._gateway.void,
            ledger=None,  # a released hold moved no money
            event_type=None,
        )

    async def refund(self, order_id: str) -> PaymentOutcome:
        return await self._lifecycle(
            order_id,
            op="refund",
            expected="CAPTURED",
            target="REFUNDED",
            gateway_call=self._gateway.refund,
            ledger=("platform_cash", "customer"),  # the reversing pair
            event_type="RefundProcessed",
        )

    async def _lifecycle(
        self,
        order_id: str,
        *,
        op: str,
        expected: str,
        target: str,
        gateway_call: Any,
        ledger: tuple[str, str] | None,
        event_type: str | None,
    ) -> PaymentOutcome:
        key = f"{order_id}:{op}"
        reserved = await self._reserve(key, _op_hash(op, order_id))
        if reserved is not None:
            return reserved

        async with self._sessions() as session:
            row = await PaymentRepo(session).get_payment(order_id)
        if row is None or row.status != expected:
            await self._store.release(MONEY_SCOPE, key)
            raise PaymentStateConflict(f"cannot {op}: payment is {row.status if row else 'absent'}")

        try:
            await gateway_call(key=key, psp_ref=row.payment_intent_id)
        except PspUnavailable:
            await self._store.release(MONEY_SCOPE, key)
            raise
        except PspStateConflict:
            # The PSP's book disagrees with our row — surface loudly as a
            # state conflict; ops reconciles (webhooks arrive with real PSPs).
            await self._store.release(MONEY_SCOPE, key)
            raise PaymentStateConflict(f"psp refused {op}") from None

        now = _now()
        body = {"order_id": order_id, "status": target}
        async with self._sessions() as session:
            repo = PaymentRepo(session)
            version = await repo.transition_payment(
                order_id, expected=expected, target=target, now=now
            )
            if version is None:
                # Raced by another instance between our read and this write;
                # converge on whatever the winner wrote.
                existing = await repo.get_payment(order_id)
                assert existing is not None
                status, body = self._describe(existing)
                await self._store.complete(session, MONEY_SCOPE, key, status, body)
                await session.commit()
                return PaymentOutcome(status, body)
            if ledger is not None:
                debit_account, credit_account = ledger
                await repo.insert_ledger_pair(
                    order_id=order_id,
                    op_key=key,
                    debit_account=debit_account,
                    credit_account=credit_account,
                    amount_cents=row.amount_cents,  # from the STORED auth
                    currency=row.currency,
                    now=now,
                )
            if event_type is not None:
                await repo.stage_event(
                    order_id=order_id,
                    version=version,
                    event_type=event_type,
                    payload={
                        "order_id": order_id,
                        "status": target,
                        "amount_cents": row.amount_cents,
                        "currency": row.currency,
                    },
                    now=now,
                )
            await self._store.complete(session, MONEY_SCOPE, key, 200, body)
            await session.commit()
        return PaymentOutcome(200, body)

    # ── helpers ────────────────────────────────────────────────────

    async def _reserve(self, key: str, op_hash: str) -> PaymentOutcome | None:
        """None = Reserved (proceed). Otherwise the outcome to return/raise."""
        outcome = await self._store.reserve(MONEY_SCOPE, key, op_hash)
        if isinstance(outcome, Reserved):
            return None
        if isinstance(outcome, Replay):
            return PaymentOutcome(outcome.response_status, outcome.response_body, replayed=True)
        if isinstance(outcome, InProgress):
            raise MoneyOpInProgress(key)
        assert isinstance(outcome, BodyMismatch)
        raise MoneyKeyMismatch(key)

    @staticmethod
    def _describe(row: Any) -> tuple[int, dict[str, Any]]:
        """A payment row as the response it implies (convergence paths)."""
        if row.status == "DECLINED":
            return 402, {"error": {"code": "PAYMENT_DECLINED", "message": "card declined"}}
        return 200, {
            "order_id": row.order_id,
            "status": row.status,
            "amount_cents": row.amount_cents,
            "payment_intent_id": row.payment_intent_id,
        }
