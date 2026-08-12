"""The guarded transition helper: apply, idempotent replay, illegal move,
reason column, and the same-tx full-state event."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from order.adapters.repo import OrderRepo
from order.db import metadata, orders, outbox
from order.domain.transitions import IllegalTransition, transition
from smartfood_kafka import EventType
from smartfood_outbox import event_id
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def _sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_order(sessions, order_id="ord_1"):
    now = datetime.now(UTC)
    async with sessions() as s:
        await OrderRepo(s).insert_order(
            order_id=order_id,
            user_id="usr_1",
            restaurant_id="rst_1",
            restaurant_name="Biryani House",
            card_token="tok_ok",
            menu_version=3,
            pricing_snapshot={
                "subtotal_cents": 3000,
                "discount_cents": 0,
                "fee_cents": 199,
                "tax_cents": 247,
                "total_cents": 3446,
                "currency": "USD",
            },
            address_snapshot={"address_id": "adr_1", "line1": "12 Mango St", "city": "S"},
            lines=[
                {
                    "menu_item_id": "itm_a",
                    "name": "Chicken Biryani",
                    "unit_price_cents": 1500,
                    "qty": 2,
                    "options": [],
                    "line_total_cents": 3000,
                }
            ],
            now=now,
        )
        await s.commit()


async def _status(sessions, order_id="ord_1"):
    async with sessions() as s:
        row = (
            await s.execute(
                sa.select(orders.c.status, orders.c.aggregate_version, orders.c.cancel_reason)
            )
        ).one()
    return row


async def test_apply_bumps_version():
    sessions = await _sessions()
    await _seed_order(sessions)
    result = await transition(sessions, "ord_1", expected="PLACED", target="VALIDATED")
    assert result.applied and result.version == 1
    row = await _status(sessions)
    assert (row.status, row.aggregate_version) == ("VALIDATED", 1)


async def test_replay_is_idempotent_noop():
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="VALIDATED")
    replay = await transition(sessions, "ord_1", expected="PLACED", target="VALIDATED")
    assert not replay.applied  # already at target — a retried activity
    assert replay.version == 1  # nothing bumped twice


async def test_illegal_transition_names_the_actual_state():
    sessions = await _sessions()
    await _seed_order(sessions)
    with pytest.raises(IllegalTransition) as exc:
        await transition(sessions, "ord_1", expected="CONFIRMED", target="ACCEPTED")
    assert exc.value.actual == "PLACED"
    with pytest.raises(IllegalTransition) as exc:
        await transition(sessions, "ord_ghost", expected="PLACED", target="VALIDATED")
    assert exc.value.actual == "absent"


async def test_event_stages_in_the_same_transaction_with_full_state():
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="CANCELLING")
    await transition(
        sessions,
        "ord_1",
        expected="CANCELLING",
        target="CANCELLED",
        event=EventType.ORDER_CANCELLED,
        cancel_reason="item_unavailable",
    )
    async with sessions() as s:
        event = (await s.execute(sa.select(outbox))).one()
    assert event.id == event_id("order", "ord_1", 2, EventType.ORDER_CANCELLED)
    assert event.payload["status"] == "CANCELLED"
    assert event.payload["cancel_reason"] == "item_unavailable"
    assert event.payload["items"][0]["name"] == "Chicken Biryani"  # full state
    assert event.payload["totals"]["total_cents"] == 3446
    row = await _status(sessions)
    assert row.cancel_reason == "item_unavailable"
