"""Activities against real sqlite state with fake service clients: every
outcome branch, every transition effect, the non-retryable classification."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from order.activities import OrderActivities
from order.adapters.repo import OrderRepo
from order.db import metadata, orders, outbox
from order.domain.ports import PaymentStateConflict
from order.domain.transitions import transition
from order.values import AuthResult, CancelReason, LineSpec, PriceResult, ReserveResult
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
    return OrderActivities(sessions, inventory, payment), sessions, inventory, payment


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


async def test_price_order_loads_the_snapshot_never_recomputes():
    acts, _, _, _ = await _setup()
    price = await acts.price_order("ord_1")
    assert price == _price()  # exactly the placement snapshot (flag #2)


async def test_price_order_unknown_order_is_non_retryable():
    acts, _, _, _ = await _setup()
    with pytest.raises(ApplicationError) as exc:
        await acts.price_order("ord_ghost")
    assert exc.value.non_retryable


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
