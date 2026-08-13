"""All payment SQL. Guards in WHERE clauses; the ledger is insert-only —
there is deliberately NO update/delete helper for it in this file, and a
source-scan test keeps it that way."""

import uuid
from datetime import datetime
from typing import Any, cast

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
                version=0,
                created_at=now,
                updated_at=now,
            )
        )

    async def transition_payment(
        self, order_id: str, *, expected: PaymentStatus, target: PaymentStatus, now: datetime
    ) -> int | None:
        """Guarded state move; returns the NEW version, or None (0 rows —
        it wasn't in `expected`, someone else already moved it)."""
        result = await self._s.execute(
            payments.update()
            .where((payments.c.order_id == order_id) & (payments.c.status == expected))
            .values(status=target, version=payments.c.version + 1, updated_at=now)
            .returning(payments.c.version)
        )
        row = result.one_or_none()
        return None if row is None else cast(int, row.version)

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
        version: int,
        event_type: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await stage_outbox_event(
            self._s,
            outbox,
            aggregate_type="payment",
            aggregate_id=order_id,
            version=version,
            event_type=event_type,
            payload=payload,
            now=now,
        )
