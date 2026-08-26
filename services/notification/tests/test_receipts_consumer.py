"""OrderSettled through the InboxHandler: the claim check joins the event's
transaction, the nudge fires post-commit exactly once, and every failure
mode degrades to the sweeper instead of failing the batch."""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from notification import receipt_queue
from notification.consumers import InboxHandler
from notification.db import metadata, notifications, receipts
from smartfood_otel import REGISTRY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

OCCURRED = datetime(2026, 8, 25, 1, 30, tzinfo=UTC)


async def _handler():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return InboxHandler(sessions), sessions


def _settled_event(event_id="evt-s1"):
    return {
        "event_id": event_id,
        "event_type": "OrderSettled",
        "aggregate_type": "order",
        "aggregate_id": "ord_1",
        "aggregate_version": 9,
        "occurred_at": OCCURRED,
        "cell_id": "c1",
        "payload": json.dumps(
            {
                "order_id": "ord_1",
                "user_id": "usr_1",
                "restaurant_id": "rst_1",
                "restaurant_name": "Biryani House",
                "status": "SETTLED",
                "items": [
                    {
                        "name": "Chicken Biryani",
                        "qty": 2,
                        "unit_price_cents": 1299,
                        "line_total_cents": 2598,
                    }
                ],
                "totals": {
                    "subtotal_cents": 2598,
                    "discount_cents": 0,
                    "fee_cents": 299,
                    "tax_cents": 207,
                    "total_cents": 3104,
                    "currency": "USD",
                },
                "cancel_reason": None,
            }
        ),
    }


async def _rows(sessions, table):
    async with sessions() as s:
        return (await s.execute(sa.select(table))).all()


async def test_settle_mints_the_claim_check_and_nudges_once():
    handler, sessions = await _handler()
    enqueued: list[str] = []
    receipt_queue.set_queue(enqueued.append)
    try:
        await handler.handle(_settled_event())
    finally:
        receipt_queue.reset_queue()
    (row,) = await _rows(sessions, receipts)
    assert row.order_id == "ord_1" and row.user_id == "usr_1"
    assert row.totals["total_cents"] == 3104  # the payload, verbatim
    assert row.s3_key is None and row.failed_at is None  # the tasks' columns start empty
    assert enqueued == ["ord_1"]
    # The bell's silence on settlement stands: a receipt is a document,
    # not an inbox row.
    assert await _rows(sessions, notifications) == []


async def test_replayed_settle_owes_nothing():
    """At-least-once redelivery: the PK collision reports minted=False, so
    no second nudge — the first delivery (or the sweeper) owns the send."""
    handler, sessions = await _handler()
    enqueued: list[str] = []
    receipt_queue.set_queue(enqueued.append)
    try:
        await handler.handle(_settled_event())
        await handler.handle(_settled_event())
    finally:
        receipt_queue.reset_queue()
    assert len(await _rows(sessions, receipts)) == 1
    assert enqueued == ["ord_1"]


async def test_other_order_events_mint_no_receipt():
    handler, sessions = await _handler()
    event = _settled_event()
    event["event_type"] = "OrderDelivered"
    await handler.handle(event)
    assert await _rows(sessions, receipts) == []


async def test_broken_queue_never_fails_the_batch():
    """The nudge is best-effort by contract: a dead broker is COUNTED and
    swallowed (the sweeper repairs it); the row itself must still land."""

    def _boom(order_id: str) -> None:
        raise RuntimeError("amqp is on fire")

    before = REGISTRY.get_sample_value("receipt_enqueue_failures_total") or 0.0
    handler, sessions = await _handler()
    receipt_queue.set_queue(_boom)
    try:
        await handler.handle(_settled_event())  # must not raise
    finally:
        receipt_queue.reset_queue()
    assert len(await _rows(sessions, receipts)) == 1
    assert REGISTRY.get_sample_value("receipt_enqueue_failures_total") == before + 1.0


async def test_unarmed_queue_is_a_quiet_noop():
    """No broker configured (the disarm idiom): rows still mint — the
    sweeper in a later armed deployment could still deliver them."""
    handler, sessions = await _handler()
    receipt_queue.reset_queue()
    await handler.handle(_settled_event())
    assert len(await _rows(sessions, receipts)) == 1
