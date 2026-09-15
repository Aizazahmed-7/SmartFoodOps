"""InboxHandler against real sqlite: natural-key dedupe, and the refund
hand-off to Temporal (ADR-0040 — the workflow itself has its own suite)."""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from notification.consumers import GROUP_ORDERS, GROUP_PAYMENTS, InboxHandler
from notification.db import metadata, notifications
from notification.values import RefundNotice
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
    refunds = RecordingRefunds()
    return InboxHandler(sessions, refunds), sessions, refunds


class RecordingRefunds:
    """Stands in for the Temporal starter — the handler's whole job on the
    payments topic is to hand the notice over, so that is what we assert."""

    def __init__(self) -> None:
        self.started: list[RefundNotice] = []

    async def notify_refund(self, notice: RefundNotice) -> None:
        self.started.append(notice)


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
    handler, sessions, refunds = await _handler()
    await handler.handle(_order_event())
    rows = await _rows(sessions)
    assert {(r.recipient_type, r.recipient_id) for r in rows} == {
        ("restaurant", "rst_1"),
        ("customer", "usr_1"),
    }
    assert all(r.order_id == "ord_1" and r.read_at is None for r in rows)
    assert all(r.created_at.replace(tzinfo=UTC) == OCCURRED for r in rows)
    assert refunds.started == []  # order events never reach the workflow


async def test_redelivery_is_a_noop():
    """NATURAL_KEY dedupe: the deterministic id collides on the PK and the
    conflict-ignore insert absorbs the replay — no processed_events table."""
    handler, sessions, refunds = await _handler()
    await handler.handle(_order_event())
    await handler.handle(_order_event())  # at-least-once redelivery
    assert len(await _rows(sessions)) == 2  # still one per recipient


async def test_silent_order_events_mint_nothing():
    """OrderPlaced produces no drafts. It no longer needs to do anything
    else either — the recipients projection it used to arm is gone."""
    handler, sessions, refunds = await _handler()
    await handler.handle(_order_event("OrderPlaced", status="PLACED"))
    assert await _rows(sessions) == []
    assert refunds.started == []


async def test_refund_is_handed_to_the_workflow_not_minted_here():
    """The handler does NOT write the row: the recipient is not in this
    payload, so the workflow looks it up and Temporal owns that retry."""
    handler, sessions, refunds = await _handler()
    await handler.handle(_refund_event())
    assert await _rows(sessions) == []  # nothing minted synchronously
    (notice,) = refunds.started
    assert (notice.order_id, notice.amount_cents, notice.currency) == ("ord_1", 1200, "USD")
    assert notice.event_id == "evt-r1"  # the notification id derives from it


async def test_refund_needs_no_prior_order_event():
    """The point of the change: a refund that beats every order event
    through the pipeline is handled on its own, with no projection to wait
    for and nothing to park on the DLQ."""
    handler, sessions, refunds = await _handler()
    await handler.handle(_refund_event())  # nothing else has ever arrived
    assert len(refunds.started) == 1


async def test_foreign_aggregates_are_ignored():
    handler, sessions, refunds = await _handler()
    event = _order_event()
    event["aggregate_type"] = "stock"
    await handler.handle(event)
    assert await _rows(sessions) == []
    assert refunds.started == []


async def test_silent_payment_events_start_no_workflow():
    """PaymentAuthorized/Captured mint nothing, so they must not start a
    workflow either — one Temporal execution per silent payment fact would
    be the most expensive no-op in the system."""
    handler, sessions, refunds = await _handler()
    for event_type in ("PaymentAuthorized", "PaymentCaptured"):
        event = _refund_event(event_id=f"evt-{event_type}")
        event["event_type"] = event_type
        await handler.handle(event)
    assert await _rows(sessions) == []
    assert refunds.started == []


async def test_refund_parks_when_temporal_is_unreachable_and_replays_clean():
    """The residual coupling, stated honestly (ADR-0040): the handler still
    has to reach Temporal to hand the work over. If that fails the event
    parks — visible and replayable — rather than vanishing. The retry
    horizon it buys is for ORDER being down, which is the long outage; a
    Temporal outage is the same risk placement already accepts."""
    from smartfood_kafka import EventConsumer
    from smartfood_kafka.testing import StubDlq, StubKafkaConsumer, StubMessage, StubSerde

    handler, sessions, refunds = await _handler()

    class Unreachable:
        async def notify_refund(self, notice):
            raise ConnectionError("temporal unreachable")

    handler._refunds = Unreachable()

    value = json.dumps(_refund_event()).encode()  # occurred_at is already ISO
    dlq = StubDlq()
    payments_client = StubKafkaConsumer([StubMessage(value=value, topic="c1.payments.events")])
    await EventConsumer(
        "c1.payments.events",
        GROUP_PAYMENTS,
        handler,
        StubSerde(),
        client=payments_client,
        dlq=dlq,
        max_attempts=2,
        backoff_seconds=0.0,
    ).consume_once()
    (topic, parked, _, headers) = dlq.parked[0]
    assert topic == "c1.payments.events.dlq"
    assert dict(headers)["dlq.error.type"] == b"ConnectionError"
    assert payments_client.commits == 1  # the partition moved on
    assert refunds.started == []

    # Temporal comes back; the ops replay of the parked bytes hands it over.
    handler._refunds = refunds
    await EventConsumer(
        "c1.payments.events",
        GROUP_PAYMENTS,
        handler,
        StubSerde(),
        client=StubKafkaConsumer([StubMessage(value=parked, topic="c1.payments.events")]),
        dlq=StubDlq(),
    ).consume_once()
    assert [n.order_id for n in refunds.started] == ["ord_1"]


def test_group_names_are_pinned():
    """The group ids ARE the consumers' offset ledgers — renaming one
    silently replays every retained event through the inbox. And they must
    DIFFER: separate groups mean separate offsets, so a poison event
    parking on one topic cannot stall the other's loop."""
    assert GROUP_ORDERS == "notification.inbox.orders"
    assert GROUP_PAYMENTS == "notification.inbox.payments"
    assert GROUP_ORDERS != GROUP_PAYMENTS
