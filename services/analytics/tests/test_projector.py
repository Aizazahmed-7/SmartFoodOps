"""The facts projector: folding, idempotency, out-of-order tolerance,
and the defensive payload paths."""

import sqlalchemy as sa
from analytics.adapters.repo import _total_cents
from analytics.consumers import FactsProjector
from analytics.db import metadata, order_facts
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


async def _sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _event(event_type, order_id="ord_1", at="2026-08-24T10:00:00+00:00", **payload):
    """Payload as a JSON STRING — the actual wire shape inside the Avro
    envelope (found live: the dict-shaped fixture hid a TypeError that
    parked the whole topic)."""
    base = {
        "order_id": order_id,
        "user_id": "usr_1",
        "restaurant_id": "rst_1",
        "status": {
            "OrderPlaced": "PLACED",
            "OrderConfirmed": "CONFIRMED",
            "OrderDelivered": "DELIVERED",
            "OrderCancelled": "CANCELLED",
            "OrderSettled": "SETTLED",
        }.get(event_type, "PLACED"),
        "aggregate_version": 1,
        "totals": {"totals": {"total_cents": 4096}},
        "occurred_at": at,
        **payload,
    }
    import json

    return {"event_type": event_type, "payload": json.dumps(base)}


async def _row(sessions, order_id="ord_1"):
    async with sessions() as s:
        return (
            await s.execute(sa.select(order_facts).where(order_facts.c.order_id == order_id))
        ).one_or_none()


async def test_lifecycle_folds_into_one_row():
    sessions = await _sessions()
    projector = FactsProjector(sessions)
    await projector.handle_batch(
        [
            _event("OrderPlaced", at="2026-08-24T10:00:00+00:00"),
            _event("OrderConfirmed", at="2026-08-24T10:00:02+00:00"),
            _event("OrderDelivered", at="2026-08-24T10:20:00+00:00"),
            _event("OrderSettled", at="2026-08-24T10:20:01+00:00"),
        ]
    )
    row = await _row(sessions)
    assert row is not None and row.status == "SETTLED"
    assert row.placed_at is not None and row.delivered_at is not None
    assert row.total_cents == 4096


async def test_redelivered_batch_converges_not_doubles():
    """The property that licenses batching at all: replaying the same
    events lands on the same row with the same values."""
    sessions = await _sessions()
    projector = FactsProjector(sessions)
    batch = [_event("OrderPlaced"), _event("OrderConfirmed")]
    await projector.handle_batch(batch)
    await projector.handle_batch(batch)  # crash-before-commit redelivery
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(order_facts))).scalar_one()
    assert count == 1
    row = await _row(sessions)
    assert row is not None and row.status == "CONFIRMED"


async def test_cancellation_carries_its_reason():
    sessions = await _sessions()
    await FactsProjector(sessions).handle_batch(
        [
            _event("OrderPlaced"),
            _event("OrderCancelled", cancel_reason="restaurant_rejected"),
        ]
    )
    row = await _row(sessions)
    assert row is not None
    assert row.cancelled_at is not None and row.cancel_reason == "restaurant_rejected"


async def test_milestone_event_without_prior_placed_still_lands():
    """A fact row born from a mid-lifecycle event (first deploy against an
    already-flowing topic): the upsert's insert half carries the base
    columns, so nothing NULL-crashes."""
    sessions = await _sessions()
    await FactsProjector(sessions).handle_batch([_event("OrderDelivered")])
    row = await _row(sessions)
    assert row is not None and row.delivered_at is not None and row.placed_at is None


async def test_unknown_event_types_are_skipped():
    sessions = await _sessions()
    await FactsProjector(sessions).handle_batch([_event("OrderRefundLaunched")])
    assert await _row(sessions) is None


def test_total_cents_defensive_paths():
    """The snapshot nests totals inside totals; missing or garbage shapes
    read as 0, never raise — a malformed payload must cost accuracy of one
    field, not the batch."""
    assert _total_cents({"totals": {"totals": {"total_cents": 812}}}) == 812
    assert _total_cents({"totals": {"total_cents": 500}}) == 500
    assert _total_cents({"totals": None}) == 0
    assert _total_cents({}) == 0
    assert _total_cents({"totals": {"totals": {"total_cents": "NaNsense"}}}) == 0


async def test_dict_payloads_still_fold():
    """Tolerance for an already-decoded payload (tests, future producers):
    isinstance-gated, so both shapes land identically."""
    sessions = await _sessions()
    projector = FactsProjector(sessions)
    import json as _json

    event = _event("OrderPlaced")
    event["payload"] = _json.loads(event["payload"])  # pre-decoded shape
    await projector.handle_batch([event])
    row = await _row(sessions)
    assert row is not None and row.status == "PLACED"


async def test_per_message_mode_is_a_batch_of_one():
    sessions = await _sessions()
    projector = FactsProjector(sessions)
    await projector.handle(_event("OrderPlaced"))
    row = await _row(sessions)
    assert row is not None and row.status == "PLACED"


async def test_create_orders_placed_shape_folds_too():
    """The OTHER producer shape on this topic: create_order's OrderPlaced
    stamps placed_at (no occurred_at, no aggregate_version). Found live —
    the first event in the topic parked on a KeyError."""
    import json as _json

    sessions = await _sessions()
    payload = {
        "order_id": "ord_real",
        "user_id": "usr_1",
        "restaurant_id": "rst_1",
        "restaurant_name": "Biryani House",
        "status": "PLACED",
        "menu_version": 10,
        "items": [],
        "totals": {"totals": {"total_cents": 4096}},
        "delivery_address": {},
        "placed_at": "2026-08-24T10:00:00+00:00",
    }
    await FactsProjector(sessions).handle_batch(
        [{"event_type": "OrderPlaced", "payload": _json.dumps(payload)}]
    )
    row = await _row(sessions, "ord_real")
    assert row is not None and row.placed_at is not None and row.total_cents == 4096
