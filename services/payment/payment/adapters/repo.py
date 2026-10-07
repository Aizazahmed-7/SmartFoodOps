"""All payment SQL. Guards in WHERE clauses; the ledger is insert-only —
there is deliberately NO update/delete helper for it in this file, and a
source-scan test keeps it that way."""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from smartfood_outbox import stage_event as stage_outbox_event
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import PaymentStatus, ledger, outbox, payments


class PaymentRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    async def get_payment(self, order_id: str) -> Row[Any] | None:
        result = await self._s.execute(sa.select(payments).where(payments.c.order_id == order_id))
        return result.one_or_none()

    async def insert_payment(
        self,
        *,
        order_id: str,
        status: PaymentStatus,
        amount_cents: int,
        currency: str,
        card_token: str,
        payment_intent_id: str | None,
        now: datetime,
    ) -> None:
        await self._s.execute(
            payments.insert().values(
                order_id=order_id,
                status=status,
                amount_cents=amount_cents,
                currency=currency,
                card_token=card_token,
                payment_intent_id=payment_intent_id,
                created_at=now,
                updated_at=now,
            )
        )

    async def transition_payment(
        self, order_id: str, *, expected: PaymentStatus, target: PaymentStatus, now: datetime
    ) -> bool:
        """Guarded state move; False = 0 rows, meaning it wasn't in
        `expected` and someone else already moved it. Returned the new
        version until the version columns went — the caller only ever asked
        whether it applied."""
        result = await self._s.execute(
            payments.update()
            .where((payments.c.order_id == order_id) & (payments.c.status == expected))
            .values(status=target, updated_at=now)
            .returning(payments.c.order_id)
        )
        return result.one_or_none() is not None

    async def insert_ledger_pair(
        self,
        *,
        order_id: str,
        op_key: str,
        debit_account: str,
        credit_account: str,
        amount_cents: int,
        currency: str,
        now: datetime,
    ) -> None:
        """One money movement = one balanced pair, atomically."""
        await self._s.execute(
            ledger.insert(),
            [
                {
                    "entry_id": f"led_{uuid.uuid4().hex}",
                    "order_id": order_id,
                    "op_key": op_key,
                    "account": debit_account,
                    "debit_cents": amount_cents,
                    "credit_cents": 0,
                    "currency": currency,
                    "created_at": now,
                },
                {
                    "entry_id": f"led_{uuid.uuid4().hex}",
                    "order_id": order_id,
                    "op_key": op_key,
                    "account": credit_account,
                    "debit_cents": 0,
                    "credit_cents": amount_cents,
                    "currency": currency,
                    "created_at": now,
                },
            ],
        )

    async def stage_event(
        self,
        *,
        order_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="payment",
            aggregate_id=order_id,
            event_type=event_type,
            payload=payload,
            now=now,
        )
