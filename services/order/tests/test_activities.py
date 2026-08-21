"""Activities against real sqlite state with fake service clients: every
outcome branch, every transition effect, the non-retryable classification."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from order.activities import OrderActivities
from order.adapters.repo import OrderRepo
from order.db import metadata, order_items, orders, outbox
from order.domain.ports import PaymentStateConflict
from order.domain.transitions import transition
from order.values import (
    AuthResult,
    CancelReason,
    LineSnapshot,
    LineSpec,
    PlacementInput,
    PriceResult,
    ReserveResult,
)
from smartfood_kafka import EventType
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from temporalio.exceptions import ApplicationError


class FakeInventory:
    def __init__(self):
        self.reserve_result: ReserveResult = "ok"
        self.calls: list = []

    async def reserve(self, *, order_id, restaurant_id, lines):
        line_pairs = [(li.item_id, li.qty) for li in lines]
        self.calls.append(("reserve", order_id, restaurant_id, line_pairs))
        return self.reserve_result

    async def release(self, order_id, *, reason="cancelled"):
        self.calls.append(("release", order_id, reason))

    async def commit(self, order_id):
        self.calls.append(("commit", order_id))


class FakePayment:
    def __init__(self):
        self.auth_result: AuthResult = "ok"
        self.capture_fail: Exception | None = None
        self.calls: list = []

    async def authorize(self, order_id, *, amount_cents, currency, card_token):
        self.calls.append(("authorize", order_id, amount_cents, card_token))
        return self.auth_result

    async def void(self, order_id):
        self.calls.append(("void", order_id))

    async def capture(self, order_id):
        self.calls.append(("capture", order_id))
        if self.capture_fail is not None:
            raise self.capture_fail

    async def refund(self, order_id):
        self.calls.append(("refund", order_id))


async def _setup():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        await OrderRepo(s).insert_order(
            order_id="ord_1",
            user_id="usr_1",
            restaurant_id="rst_1",
            restaurant_name="Biryani House",
            card_token="tok_ok",
            request_hash="hash-of-K-1",
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
            now=datetime.now(UTC),
        )
        await s.commit()
    inventory, payment = FakeInventory(), FakePayment()
    acts = OrderActivities(sessions, inventory, payment)
    return acts, sessions, inventory, payment


async def _status(sessions):
    async with sessions() as s:
        return (await s.execute(sa.select(orders.c.status))).scalar_one()


def _price():
    return PriceResult(
        restaurant_id="rst_1",
        amount_cents=3446,
        currency="USD",
        card_token="tok_ok",
        lines=[LineSpec(item_id="itm_a", qty=2)],
    )


def _placement(order_id="ord_2", key="K-2"):
    return PlacementInput(
        order_id=order_id,
        request_hash=f"hash-of-{key}",
        user_id="usr_1",
        restaurant_id="rst_1",
        restaurant_name="Biryani House",
        card_token="tok_ok",
        menu_version=3,
        currency="USD",
        amount_cents=3446,
        placed_at=datetime.now(UTC).isoformat(),
        lines=[
            LineSnapshot(
                menu_item_id="itm_a",
                name="Chicken Biryani",
                unit_price_cents=1500,
                qty=2,
                line_total_cents=3000,
            )
        ],
        pricing_snapshot={"total_cents": 3446, "currency": "USD"},
        address_snapshot={"address_id": "adr_1", "line1": "12 Mango St", "city": "S"},
    )


async def test_create_order_writes_the_row_lines_and_event():
    """The activity IS placement: order + lines + OrderPlaced in one
    commit, with request_hash stamped on the row — the row is the whole
    idempotency record now (ADR-0024)."""
    acts, sessions, _, _ = await _setup()
    placement = _placement()

    assert await acts.create_order(placement) == "PLACED"

    async with sessions() as s:
        order = (await s.execute(sa.select(orders).where(orders.c.order_id == "ord_2"))).one()
        items = (
            await s.execute(sa.select(order_items).where(order_items.c.order_id == "ord_2"))
        ).all()
        event = (await s.execute(sa.select(outbox).where(outbox.c.aggregate_id == "ord_2"))).one()
    assert (order.status, order.aggregate_version) == ("PLACED", 0)
    assert order.restaurant_name_snapshot == "Biryani House"
    assert order.request_hash == "hash-of-K-2"  # the body this order answers for
    assert len(items) == 1 and items[0].name_snapshot == "Chicken Biryani"
    assert event.event_type == EventType.ORDER_PLACED and event.payload["status"] == "PLACED"


async def test_create_order_run_twice_makes_exactly_one_order():
    """At-least-once, survived: an activity that commits and then loses its
    worker is retried with the SAME input. The second run must adopt the
    row rather than raise or duplicate — this is the property the derived
    order id buys."""
    acts, sessions, _, _ = await _setup()
    placement = _placement()

    assert await acts.create_order(placement) == "PLACED"
    assert await acts.create_order(placement) == "PLACED"  # the retry

    async with sessions() as s:
        count = (
            await s.execute(
                sa.select(sa.func.count()).select_from(orders).where(orders.c.order_id == "ord_2")
            )
        ).scalar_one()
        events = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(outbox)
                .where(outbox.c.aggregate_id == "ord_2")
            )
        ).scalar_one()
    assert (count, events) == (1, 1)  # one order, one OrderPlaced fact


async def test_create_order_retry_reports_the_rows_real_status():
    """If the saga moved on before the retry landed, the answer handed back
    to any waiting request is the row's ACTUAL state, never a hopeful
    'PLACED' the database would contradict."""
    acts, sessions, _, _ = await _setup()
    placement = _placement()
    await acts.create_order(placement)
    await transition(sessions, "ord_2", expected="PLACED", target="VALIDATED")

    assert await acts.create_order(placement) == "VALIDATED"


async def test_reserve_ok_transitions_to_validated():
    acts, sessions, inventory, _ = await _setup()
    assert await acts.validate_and_reserve("ord_1", _price()) == "ok"
    assert await _status(sessions) == "VALIDATED"
    assert inventory.calls == [("reserve", "ord_1", "rst_1", [("itm_a", 2)])]


async def test_reserve_failure_returns_value_and_leaves_placed():
    acts, sessions, inventory, _ = await _setup()
    inventory.reserve_result = "item_unavailable"
    assert await acts.validate_and_reserve("ord_1", _price()) == "item_unavailable"
    assert await _status(sessions) == "PLACED"  # value, not transition


async def test_authorize_ok_transitions_to_payment_cleared():
    acts, sessions, _, payment = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    assert await acts.authorize_payment("ord_1", _price()) == "ok"
    assert await _status(sessions) == "PAYMENT_CLEARED"
    assert payment.calls == [("authorize", "ord_1", 3446, "tok_ok")]


async def test_authorize_declined_stays_validated():
    acts, sessions, _, payment = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    payment.auth_result = "declined"
    assert await acts.authorize_payment("ord_1", _price()) == "declined"
    assert await _status(sessions) == "VALIDATED"


async def test_confirm_stages_the_full_state_event():
    acts, sessions, _, _ = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    await acts.authorize_payment("ord_1", _price())
    await acts.confirm_order("ord_1")
    assert await _status(sessions) == "CONFIRMED"
    async with sessions() as s:
        event = (await s.execute(sa.select(outbox))).one()
    assert event.event_type == EventType.ORDER_CONFIRMED
    assert event.payload["totals"]["total_cents"] == 3446


async def test_full_cancel_path_writes_reason_and_event():
    acts, sessions, inventory, payment = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    await acts.begin_cancel("ord_1", "VALIDATED", CancelReason.PAYMENT_DECLINED)
    async with sessions() as s:
        # the reason lands at BEGIN — the CANCELLING window must already
        # carry it (the kitchen's decision matrix classifies from it)
        mid = (await s.execute(sa.select(orders.c.cancel_reason))).scalar_one()
    assert mid == "payment_declined"
    await acts.void_authorization("ord_1")
    await acts.release_reservation("ord_1")
    await acts.finish_cancel("ord_1", CancelReason.PAYMENT_DECLINED)
    assert await _status(sessions) == "CANCELLED"
    assert ("void", "ord_1") in payment.calls
    assert ("release", "ord_1", "cancelled") in inventory.calls
    async with sessions() as s:
        row = (await s.execute(sa.select(orders.c.cancel_reason))).scalar_one()
        types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    assert row == "payment_declined"
    assert EventType.ORDER_CANCELLED in types


async def test_mark_accepted_from_confirmed():
    acts, sessions, _, _ = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    await acts.authorize_payment("ord_1", _price())
    await acts.confirm_order("ord_1")
    await acts.mark_accepted("ord_1")
    assert await _status(sessions) == "ACCEPTED"


async def _to_ready(acts, sessions):
    """Drive the real saga chain to ACCEPTED, then the kitchen's two moves
    via the same transition() writer the API uses."""
    await acts.validate_and_reserve("ord_1", _price())
    await acts.authorize_payment("ord_1", _price())
    await acts.confirm_order("ord_1")
    await acts.mark_accepted("ord_1")
    await transition(sessions, "ord_1", expected="ACCEPTED", target="PREPARING")
    await transition(sessions, "ord_1", expected="PREPARING", target="READY")


async def test_pickup_and_delivered_stage_the_delivery_event():
    acts, sessions, _, _ = await _setup()
    await _to_ready(acts, sessions)
    await acts.mark_picked_up("ord_1")
    assert await _status(sessions) == "PICKED_UP"
    await acts.mark_delivered("ord_1")
    assert await _status(sessions) == "DELIVERED"
    async with sessions() as s:
        types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    assert EventType.ORDER_DELIVERED in types


async def test_capture_payment_calls_the_gateway():
    acts, _, _, payment = await _setup()
    await acts.capture_payment("ord_1")
    assert payment.calls == [("capture", "ord_1")]


async def test_capture_state_conflict_is_non_retryable():
    """No capturable auth = settling without money — fail the workflow,
    never converge, never retry."""
    acts, _, _, payment = await _setup()
    payment.capture_fail = PaymentStateConflict("no auth")
    with pytest.raises(ApplicationError) as exc:
        await acts.capture_payment("ord_1")
    assert exc.value.non_retryable
    assert exc.value.type == "PaymentStateConflict"


async def test_settle_consumes_the_reservation_and_closes_the_order():
    acts, sessions, inventory, _ = await _setup()
    await _to_ready(acts, sessions)
    await acts.mark_picked_up("ord_1")
    await acts.mark_delivered("ord_1")
    await acts.settle_order("ord_1")
    assert await _status(sessions) == "SETTLED"
    assert ("commit", "ord_1") in inventory.calls
    async with sessions() as s:
        types = [e.event_type for e in (await s.execute(sa.select(outbox))).all()]
    assert EventType.ORDER_SETTLED in types


async def test_settle_replay_is_a_noop():
    """At-least-once: a retried settle re-commits (downstream no-op) and
    lands on the transition's idempotent-replay branch."""
    acts, sessions, inventory, _ = await _setup()
    await _to_ready(acts, sessions)
    await acts.mark_picked_up("ord_1")
    await acts.mark_delivered("ord_1")
    await acts.settle_order("ord_1")
    await acts.settle_order("ord_1")  # retry — no error, still SETTLED
    assert await _status(sessions) == "SETTLED"
    assert inventory.calls.count(("commit", "ord_1")) == 2  # idempotent downstream


async def test_illegal_transition_is_non_retryable():
    acts, _, _, _ = await _setup()
    with pytest.raises(ApplicationError) as exc:
        await acts.mark_accepted("ord_1")  # PLACED, not CONFIRMED
    assert exc.value.non_retryable
    assert exc.value.type == "IllegalTransition"


async def test_try_begin_cancel_wins_from_kitchen_states_and_stamps_reason():
    acts, sessions, _, _ = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    await acts.authorize_payment("ord_1", _price())
    await acts.confirm_order("ord_1")
    await acts.mark_accepted("ord_1")
    assert await acts.try_begin_cancel("ord_1", CancelReason.CUSTOMER_CANCELLED) == "ok"
    assert await _status(sessions) == "CANCELLING"
    async with sessions() as s:
        reason = (await s.execute(sa.select(orders.c.cancel_reason))).scalar_one()
    assert reason == "customer_cancelled"
    # at-least-once replay: still "ok", no error
    assert await acts.try_begin_cancel("ord_1", CancelReason.CUSTOMER_CANCELLED) == "ok"


async def test_try_begin_cancel_loses_once_the_courier_has_it():
    acts, sessions, _, _ = await _setup()
    await _to_ready(acts, sessions)
    await acts.mark_picked_up("ord_1")
    assert await acts.try_begin_cancel("ord_1", CancelReason.CUSTOMER_CANCELLED) == "too_late"
    assert await _status(sessions) == "PICKED_UP"  # untouched — a value, not an error


async def test_every_activity_name_is_registered_exactly_once():
    """The tripwire the worker needs: workflows dispatch BY NAME, so a
    registration dropped from all() fails only at invocation time in a
    live stack — this pins all() to the ActivityName enum instead."""
    from order.values import ActivityName
    from temporalio import activity as temporal_activity

    acts, _, _, _ = await _setup()
    definitions = [temporal_activity._Definition.from_callable(fn) for fn in acts.all()]
    registered = [d.name for d in definitions if d is not None and d.name is not None]
    assert sorted(registered) == sorted(m.value for m in ActivityName)


async def test_replayed_activity_transition_is_a_noop():
    """The at-least-once story: a retried activity re-runs its transition
    and lands on the idempotent-replay branch."""
    acts, sessions, _, _ = await _setup()
    await acts.validate_and_reserve("ord_1", _price())
    await acts.validate_and_reserve("ord_1", _price())  # retry — no error
    assert await _status(sessions) == "VALIDATED"


async def test_saga_outcome_counters_move_on_settle_and_cancel():
    """order_saga_outcomes_total is counted where outcomes become FINAL —
    the worker's activities — so cancel-reason spikes are graphable."""
    from smartfood_otel import REGISTRY

    def count(outcome, reason):
        return (
            REGISTRY.get_sample_value(
                "order_saga_outcomes_total", {"outcome": outcome, "reason": reason}
            )
            or 0.0
        )

    settled_before = count("settled", "")
    declined_before = count("cancelled", "payment_declined")

    from order.db import OrderStatus

    acts, sessions, _, _ = await _setup()
    walk: list[tuple[OrderStatus, OrderStatus]] = [
        ("PLACED", "VALIDATED"),
        ("VALIDATED", "PAYMENT_CLEARED"),
        ("PAYMENT_CLEARED", "CONFIRMED"),
        ("CONFIRMED", "ACCEPTED"),
        ("ACCEPTED", "PREPARING"),
        ("PREPARING", "READY"),
        ("READY", "PICKED_UP"),
        ("PICKED_UP", "DELIVERED"),
    ]
    for expected, target in walk:
        await transition(sessions, "ord_1", expected=expected, target=target)
    await acts.settle_order("ord_1")
    assert count("settled", "") == settled_before + 1

    acts2, sessions2, _, _ = await _setup()
    await transition(
        sessions2,
        "ord_1",
        expected="PLACED",
        target="CANCELLING",
        cancel_reason=CancelReason.PAYMENT_DECLINED,
    )
    await acts2.finish_cancel("ord_1", CancelReason.PAYMENT_DECLINED)
    assert count("cancelled", "payment_declined") == declined_before + 1
