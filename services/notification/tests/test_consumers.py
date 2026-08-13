"""InboxHandler against real sqlite: projection upkeep, natural-key dedupe,
and the deliberate ProjectionLag raise (the loop that feeds it is
smartfood_kafka.EventConsumer, tested in its own lib)."""

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from notification.consumers import GROUP_ORDERS, GROUP_PAYMENTS, InboxHandler, ProjectionLag
from notification.db import metadata, notifications, order_recipients
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

OCCURRED = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


async def _handler():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return InboxHandler(sessions), sessions


def _order_event(event_type="OrderConfirmed", *, event_id="evt-1", **payload_overrides):
    payload = {
        "order_id": "ord_1",
        "user_id": "usr_1",
        "restaurant_id": "rst_1",
        "restaurant_name": "Biryani House",
        "status": "CONFIRMED",
        "items": [{"menu_item_id": "itm_1", "qty": 1}],
        "totals": {"total_cents": 1200, "currency": "USD"},
        "cancel_reason": None,
    }
    payload.update(payload_overrides)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": "order",
        "aggregate_id": "ord_1",
        "aggregate_version": 3,
        "occurred_at": OCCURRED,  # Avro decode yields aware datetimes
        "cell_id": "c1",
        "payload": json.dumps(payload),
    }


def _refund_event(event_id="evt-r1"):
    return {
        "event_id": event_id,
        "event_type": "RefundProcessed",
        "aggregate_type": "payment",
        "aggregate_id": "ord_1",
        "aggregate_version": 2,
        "occurred_at": OCCURRED.isoformat(),  # the string form must work too
        "cell_id": "c1",
        "payload": json.dumps(
            {"order_id": "ord_1", "status": "REFUNDED", "amount_cents": 1200, "currency": "USD"}
        ),
    }


async def _rows(sessions, table=notifications):
    async with sessions() as s:
        return (await s.execute(sa.select(table))).all()


async def test_order_event_mints_rows_and_projects_recipients():
    handler, sessions = await _handler()
    await handler.handle(_order_event())
    rows = await _rows(sessions)
    assert {(r.recipient_type, r.recipient_id) for r in rows} == {
        ("restaurant", "rst_1"),
        ("customer", "usr_1"),
    }
    assert all(r.order_id == "ord_1" and r.read_at is None for r in rows)
    assert all(r.created_at.replace(tzinfo=UTC) == OCCURRED for r in rows)
    projection = await _rows(sessions, order_recipients)
    assert [(p.order_id, p.user_id, p.restaurant_id) for p in projection] == [
        ("ord_1", "usr_1", "rst_1")
    ]


async def test_redelivery_is_a_noop():
    """NATURAL_KEY dedupe: the deterministic id collides on the PK and the
    conflict-ignore insert absorbs the replay — no processed_events table."""
    handler, sessions = await _handler()
    await handler.handle(_order_event())
    await handler.handle(_order_event())  # at-least-once redelivery
    assert len(await _rows(sessions)) == 2  # still one per recipient


async def test_silent_events_still_arm_the_projection():
    """OrderPlaced mints nothing but MUST project recipients — it is
    usually the first order fact to arrive, and the payment join needs it."""
    handler, sessions = await _handler()
    await handler.handle(_order_event("OrderPlaced", status="PLACED"))
    assert await _rows(sessions) == []
    assert len(await _rows(sessions, order_recipients)) == 1


async def test_refund_joins_user_through_the_projection():
    handler, sessions = await _handler()
    await handler.handle(_order_event("OrderPlaced", status="PLACED"))
    await handler.handle(_refund_event())
    (row,) = await _rows(sessions)
    assert (row.recipient_type, row.recipient_id, row.kind) == (
        "customer",
        "usr_1",
        "refund_processed",
    )


async def test_refund_before_any_order_event_raises_projection_lag():
    """Cross-topic race: the runtime's retry absorbs it; a true orphan is
    a DLQ row, never a silently-eaten refund notification."""
    handler, sessions = await _handler()
    with pytest.raises(ProjectionLag):
        await handler.handle(_refund_event())
    assert await _rows(sessions) == []


async def test_foreign_aggregates_are_ignored():
    handler, sessions = await _handler()
    event = _order_event()
    event["aggregate_type"] = "stock"
    await handler.handle(event)
    assert await _rows(sessions) == []
    assert await _rows(sessions, order_recipients) == []


async def test_silent_payment_events_never_touch_the_projection():
    """PaymentAuthorized/Captured mint nothing, so they must not consult
    order_recipients — a no-op event tripping ProjectionLag would burn a
    retry cycle and park a perfectly healthy fact on the DLQ."""
    handler, sessions = await _handler()
    for event_type in ("PaymentAuthorized", "PaymentCaptured"):
        event = _refund_event(event_id=f"evt-{event_type}")
        event["event_type"] = event_type
        await handler.handle(event)  # no ProjectionLag despite the empty projection
    assert await _rows(sessions) == []


async def test_parked_refund_replays_clean_after_orders_catch_up():
    """The full DLQ story through the REAL runtime: a refund beats its
    order (parked with ProjectionLag), the orders loop arms the
    projection, and an ops replay of the parked bytes mints the
    notification — dedupe-safe."""
    from smartfood_kafka import EventConsumer
    from smartfood_kafka.testing import StubDlq, StubKafkaConsumer, StubMessage, StubSerde

    handler, sessions = await _handler()

    def _msg(event: dict) -> StubMessage:
        wire = dict(event)
        wire["occurred_at"] = (
            wire["occurred_at"].isoformat()
            if not isinstance(wire["occurred_at"], str)
            else wire["occurred_at"]
        )
        return StubMessage(value=json.dumps(wire).encode(), topic="c1.payments.events")

    refund = _msg(_refund_event())
    dlq = StubDlq()
    payments_client = StubKafkaConsumer([refund])
    consumer = EventConsumer(
        "c1.payments.events",
        GROUP_PAYMENTS,
        handler,
        StubSerde(),
        client=payments_client,
        dlq=dlq,
        max_attempts=2,
        backoff_seconds=0.0,
    )
    await consumer.consume_once()
    (topic, value, _, headers) = dlq.parked[0]
    assert topic == "c1.payments.events.dlq"
    assert dict(headers)["dlq.error.type"] == b"ProjectionLag"
    assert payments_client.commits == 1  # partition moved on
    assert await _rows(sessions) == []  # nothing minted yet

    # The orders loop catches up and arms the projection…
    await handler.handle(_order_event("OrderPlaced", status="PLACED"))
    # …and the ops replay (the parked bytes, verbatim) heals the inbox.
    replay_client = StubKafkaConsumer([StubMessage(value=value, topic="c1.payments.events")])
    consumer2 = EventConsumer(
        "c1.payments.events",
        GROUP_PAYMENTS,
        handler,
        StubSerde(),
        client=replay_client,
        dlq=StubDlq(),
    )
    await consumer2.consume_once()
    (row,) = await _rows(sessions)
    assert (row.recipient_type, row.kind) == ("customer", "refund_processed")


def test_group_names_are_pinned():
    """The group ids ARE the consumers' offset ledgers — renaming one
    silently replays every retained event through the inbox. And they must
    DIFFER: a shared group across both topics would let a payments-side
    backoff block the orders loop that arms the projection."""
    assert GROUP_ORDERS == "notification.inbox.orders"
    assert GROUP_PAYMENTS == "notification.inbox.payments"
    assert GROUP_ORDERS != GROUP_PAYMENTS
