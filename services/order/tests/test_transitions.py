"""The guarded transition helper: apply, idempotent replay, illegal move,
reason column, and the same-tx full-state event."""

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from order.adapters.repo import OrderRepo
from order.db import OrderStatus, metadata, order_cancellations, orders, outbox
from order.domain.transitions import IllegalTransition, begin_cancel_from, transition
from smartfood_kafka import EventType
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
            request_hash="hash-x",
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
                sa.select(
                    orders.c.status,
                    orders.c.updated_at,
                    order_cancellations.c.reason.label("cancel_reason"),
                    order_cancellations.c.cancelled_at,
                ).select_from(
                    orders.outerjoin(
                        order_cancellations,
                        order_cancellations.c.order_id == orders.c.order_id,
                    )
                )
            )
        ).one()
    return row


async def test_apply_moves_the_status_and_stamps_it():
    sessions = await _sessions()
    await _seed_order(sessions)
    before = (await _status(sessions)).updated_at
    result = await transition(sessions, "ord_1", expected="PLACED", target="VALIDATED")
    assert result.applied
    row = await _status(sessions)
    assert row.status == "VALIDATED"
    assert row.updated_at > before


async def test_replay_is_idempotent_noop():
    """`applied=False` has to mean NOTHING was written, not just that a
    counter held still — so this asserts on the stamp the UPDATE would have
    moved. (It used to assert the version did not bump twice.)"""
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="VALIDATED")
    after_first = (await _status(sessions)).updated_at
    replay = await transition(sessions, "ord_1", expected="PLACED", target="VALIDATED")
    assert not replay.applied  # already at target — a retried activity
    assert (await _status(sessions)).updated_at == after_first  # nothing written


async def test_illegal_transition_names_the_actual_state():
    sessions = await _sessions()
    await _seed_order(sessions)
    with pytest.raises(IllegalTransition) as exc:
        await transition(sessions, "ord_1", expected="CONFIRMED", target="ACCEPTED")
    assert exc.value.actual == "PLACED"
    with pytest.raises(IllegalTransition) as exc:
        await transition(sessions, "ord_ghost", expected="PLACED", target="VALIDATED")
    assert exc.value.actual == "absent"


KITCHEN = ("ACCEPTED", "PREPARING", "READY")


async def test_begin_cancel_from_flips_any_allowed_state():
    for start in KITCHEN:
        sessions = await _sessions()
        await _seed_order(sessions)
        await transition(sessions, "ord_1", expected="PLACED", target=start)
        assert await begin_cancel_from(
            sessions, "ord_1", allowed=KITCHEN, reason="customer_cancelled"
        )
        row = await _status(sessions)
        assert (row.status, row.cancel_reason) == ("CANCELLING", "customer_cancelled")


async def test_begin_cancel_from_loses_to_the_courier():
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="PICKED_UP")
    assert not await begin_cancel_from(
        sessions, "ord_1", allowed=KITCHEN, reason="customer_cancelled"
    )
    row = await _status(sessions)
    assert (row.status, row.cancel_reason) == ("PICKED_UP", None)  # untouched


async def test_begin_cancel_from_replay_is_true_without_a_second_write():
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="READY")
    assert await begin_cancel_from(sessions, "ord_1", allowed=KITCHEN, reason="customer_cancelled")
    stamped = (await _status(sessions)).updated_at
    assert await begin_cancel_from(sessions, "ord_1", allowed=KITCHEN, reason="customer_cancelled")
    row = await _status(sessions)
    assert row.status == "CANCELLING"
    assert row.updated_at == stamped  # the set guard matched nothing the 2nd time


async def test_begin_cancel_from_unknown_order_is_false():
    sessions = await _sessions()
    assert not await begin_cancel_from(
        sessions, "ord_ghost", allowed=KITCHEN, reason="customer_cancelled"
    )


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
    assert (event.aggregate_type, event.aggregate_id) == ("order", "ord_1")
    assert event.event_type == EventType.ORDER_CANCELLED
    assert event.payload["status"] == "CANCELLED"
    assert event.payload["cancel_reason"] == "item_unavailable"
    assert event.payload["items"][0]["name"] == "Chicken Biryani"  # full state
    assert event.payload["totals"]["total_cents"] == 3446
    row = await _status(sessions)
    assert row.cancel_reason == "item_unavailable"


# ── the cancellation split (review 2026-09-15) ─────────────────────


async def test_cancelled_at_records_the_decision_not_the_finish():
    """`begin_cancel` stamps the reason at CANCELLING and the unwind can
    hold that state for an unbounded window; the CANCELLED transition
    re-stamps the SAME reason and must not move the timestamp off the
    moment the cancellation was actually decided."""
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="READY")

    assert await begin_cancel_from(sessions, "ord_1", allowed=KITCHEN, reason="customer_cancelled")
    decided = (await _status(sessions)).cancelled_at

    await transition(
        sessions,
        "ord_1",
        expected="CANCELLING",
        target="CANCELLED",
        event=EventType.ORDER_CANCELLED,
        cancel_reason="customer_cancelled",
    )
    row = await _status(sessions)
    assert row.status == "CANCELLED"
    assert row.cancel_reason == "customer_cancelled"
    assert row.cancelled_at == decided  # the second write did not move it


async def test_a_completed_order_has_no_cancellation_row():
    """The absence IS the answer — there is no NULL reason to interpret."""
    sessions = await _sessions()
    await _seed_order(sessions)
    await transition(sessions, "ord_1", expected="PLACED", target="DELIVERED")
    row = await _status(sessions)
    assert (row.status, row.cancel_reason, row.cancelled_at) == ("DELIVERED", None, None)
    async with sessions() as s:
        count = (
            await s.execute(sa.select(sa.func.count()).select_from(order_cancellations))
        ).scalar_one()
    assert count == 0


# ── milestones (FR-81) ─────────────────────────────────────────────


async def _milestones(sessions, order_id="ord_1"):
    async with sessions() as s:
        return (
            await s.execute(
                sa.select(
                    orders.c.confirmed_at,
                    orders.c.accepted_at,
                    orders.c.preparing_at,
                    orders.c.ready_at,
                    orders.c.picked_up_at,
                ).where(orders.c.order_id == order_id)
            )
        ).one()


HAPPY_PATH: tuple[tuple[OrderStatus, OrderStatus, EventType | None], ...] = (
    ("PLACED", "VALIDATED", None),
    ("VALIDATED", "PAYMENT_CLEARED", None),
    ("PAYMENT_CLEARED", "CONFIRMED", EventType.ORDER_CONFIRMED),
    ("CONFIRMED", "ACCEPTED", EventType.ORDER_ACCEPTED),
    ("ACCEPTED", "PREPARING", EventType.ORDER_PREPARING),
    ("PREPARING", "READY", EventType.ORDER_READY),
    ("READY", "PICKED_UP", EventType.ORDER_PICKED_UP),
)


async def _walk(sessions, upto, *, start="PLACED"):
    """Drive the order along the real happy path, one guarded move at a
    time — the milestones are only trustworthy if they survive the actual
    sequence, not a hand-written UPDATE."""
    reached = False
    for expected, target, event in HAPPY_PATH:
        reached = reached or expected == start
        if not reached:
            continue
        await transition(sessions, "ord_1", expected=expected, target=target, event=event)
        if target == upto:
            return


async def test_each_stage_stamps_only_its_own_column():
    """The point of four columns rather than one `updated_at`: after the
    kitchen starts cooking, the row still remembers when it accepted."""
    sessions = await _sessions()
    await _seed_order(sessions)

    await _walk(sessions, "ACCEPTED")
    row = await _milestones(sessions)
    assert row.confirmed_at is not None and row.accepted_at is not None
    assert (row.preparing_at, row.ready_at, row.picked_up_at) == (None, None, None)

    await _walk(sessions, "PICKED_UP", start="ACCEPTED")
    row = await _milestones(sessions)
    assert row.confirmed_at < row.accepted_at < row.preparing_at < row.ready_at < row.picked_up_at


async def test_a_replayed_transition_cannot_restamp_a_milestone():
    """The exactly-once claim. An at-least-once redelivery arriving minutes
    late must not move `accepted_at` forward — that is the difference
    between a fact about a moment and a field that merely holds a date."""
    sessions = await _sessions()
    await _seed_order(sessions)
    await _walk(sessions, "ACCEPTED")
    first = (await _milestones(sessions)).accepted_at

    replay = await transition(
        sessions, "ord_1", expected="CONFIRMED", target="ACCEPTED", event=EventType.ORDER_ACCEPTED
    )
    assert not replay.applied
    assert (await _milestones(sessions)).accepted_at == first


async def test_a_milestone_event_carries_the_whole_timeline():
    """Full-state means full state: a consumer joining the stream at
    OrderReady must be able to time the kitchen without replaying the topic
    from its beginning."""
    sessions = await _sessions()
    await _seed_order(sessions)
    await _walk(sessions, "READY")
    async with sessions() as s:
        row = (
            await s.execute(sa.select(outbox).where(outbox.c.event_type == EventType.ORDER_READY))
        ).one()
    payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
    timeline = payload["milestones"]
    # `accept_timeout_s` runs from here, so FR-84 cannot bound the
    # restaurant's window without it.
    assert timeline["confirmed_at"] is not None
    assert timeline["accepted_at"] is not None
    assert timeline["preparing_at"] is not None
    # The milestone THIS event announces is already in it — the row is
    # re-read after the UPDATE, inside the same transaction.
    assert timeline["ready_at"] is not None
    assert timeline["picked_up_at"] is None
    assert payload["placed_at"] is not None


async def test_terminal_moves_stamp_no_milestone_column():
    """DELIVERED/SETTLED/CANCELLED deliberately have no column — they
    announce themselves as events carrying `occurred_at`."""
    sessions = await _sessions()
    await _seed_order(sessions)
    await _walk(sessions, "PICKED_UP")
    before = await _milestones(sessions)
    await transition(
        sessions, "ord_1", expected="PICKED_UP", target="DELIVERED", event=EventType.ORDER_DELIVERED
    )
    assert (await _milestones(sessions)) == before
