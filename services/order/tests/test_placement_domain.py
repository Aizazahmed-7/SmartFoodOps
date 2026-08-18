"""Domain-level placement: the DB artifacts HTTP can't see — the
four-writes-one-transaction guarantee, snapshot contents, event identity."""

import datetime as dt

import pytest
import sqlalchemy as sa
from order.db import idempotency_keys, metadata, order_items, orders, outbox
from order.domain.ports import PlacementPending, SagaClosed, SagaUnavailable
from order.domain.service import OrderService, Placed, order_id_for
from smartfood_idempotency import IdempotencyStore, body_hash
from smartfood_outbox import event_id
from smartfood_pricing import Line, PricingConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# The catalog/identity doubles here are deliberately distinct from
# conftest's — they carry static canned answers. The SAGA double is the
# shared `saga` fixture, bound to this module's own database: placement
# runs through the real create_order activity either way (ADR-0023), and
# two different inlined workers would be two different truths.


class StaticCatalog:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def get_snapshot(self, restaurant_id, item_ids):
        return self.snapshot


class StaticIdentity:
    async def get_address(self, user_id, address_id):
        return {
            "id": address_id,
            "label": "home",
            "line1": "12 Mango St",
            "city": "Springfield",
            "lat": 24.8,
            "lon": 67.0,
        }


def _menu_items():
    """itm_a with a required Size group — pricing must fold the option in."""
    return [
        {
            "id": "itm_a",
            "name": "Chicken Biryani",
            "price_cents": 1200,
            "currency": "USD",
            "available": True,
            "modifier_groups": [
                {
                    "id": "grp_size",
                    "name": "Size",
                    "min_select": 1,
                    "max_select": 1,
                    "options": [{"id": "opt_lg", "name": "Large", "price_delta_cents": 300}],
                }
            ],
        }
    ]


async def _service(make_snapshot, saga):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    saga.bind(sessions)  # the double runs the real create_order activity
    service = OrderService(
        StaticCatalog(make_snapshot(items=_menu_items())),
        pricing=PricingConfig(delivery_fee_cents=199, tax_basis_points=825),
        sessions=sessions,
        identity=StaticIdentity(),
        saga=saga,
        idempotency=IdempotencyStore(sessions, idempotency_keys),
    )
    return service, sessions, saga


def _lines():
    return [
        Line.model_validate(
            {
                "item_id": "itm_a",
                "qty": 2,
                "options": ({"group_id": "grp_size", "option_id": "opt_lg"},),
            }
        )
    ]


async def _place(service, key="k1"):
    return await service.place(
        user_id="usr_1",
        idem_key=key,
        request_hash=body_hash(b"the-body"),
        restaurant_id="rst_1",
        menu_version=3,
        lines=_lines(),
        address_id="adr_1",
        card_token="tok_ok",
    )


async def test_placement_writes_all_four_artifacts_in_one_commit(make_snapshot, saga):
    """Unchanged guarantee, new author: the four writes now happen inside
    the saga's create_order activity, and they still commit together."""
    service, sessions, saga = await _service(make_snapshot, saga)
    outcome = await _place(service)
    assert isinstance(outcome, Placed)

    async with sessions() as s:
        order = (await s.execute(sa.select(orders))).one()
        items = (await s.execute(sa.select(order_items))).all()
        event = (await s.execute(sa.select(outbox))).one()
        idem = (await s.execute(sa.select(idempotency_keys))).one()

    # 1. the order, with every snapshot placement promised (FR-14/16)
    assert order.status == "PLACED"
    assert order.menu_version == 3
    assert order.card_token == "tok_ok"
    # (1200 + 300) * 2 = 3000; tax = 3000*825//10000 = 247
    assert order.pricing_snapshot == {
        "subtotal_cents": 3000,
        "discount_cents": 0,
        "fee_cents": 199,
        "tax_cents": 247,
        "total_cents": 3446,
        "currency": "USD",
    }
    assert order.delivery_address_snapshot["line1"] == "12 Mango St"
    assert order.delivery_address_snapshot["address_id"] == "adr_1"

    # 2. line snapshots that survive any future menu edit
    assert len(items) == 1
    line = items[0]
    assert (line.name_snapshot, line.unit_price_cents, line.qty) == ("Chicken Biryani", 1200, 2)
    assert line.options_snapshot[0]["name"] == "Large"
    assert line.line_total_cents == 3000

    # 3. the OrderPlaced event, deterministic identity, full-state payload
    assert event.id == event_id("order", order.order_id, 0, "OrderPlaced")
    assert event.payload["totals"]["total_cents"] == 3446
    assert event.payload["delivery_address"]["city"] == "Springfield"

    # 4. the idempotency row completed IN the same transaction
    assert idem.status == "COMPLETE"
    assert idem.response_body == {"order_id": order.order_id, "status": "PLACED"}

    assert saga.placed == [order.order_id]
    # The id is DERIVED from the key, not random — this is the property the
    # whole retry story rests on (ADR-0023).
    assert order.order_id == order_id_for("usr_1", "k1")


async def test_replay_never_touches_the_database_again(make_snapshot, saga):
    service, sessions, saga = await _service(make_snapshot, saga)
    first = await _place(service)
    replay = await _place(service)  # same key, same hash
    from smartfood_idempotency import Replay

    assert isinstance(replay, Replay)
    assert replay.response_body["order_id"] == first.order_id
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one()
    assert count == 1
    assert len(saga.placed) == 1  # the saga was not asked a second time


async def test_order_ids_are_derived_from_the_key_not_random():
    """Same scope + key → same id, always; a different key → a different
    order. Randomness here would mint a second order on every takeover."""
    assert order_id_for("usr_1", "k1") == order_id_for("usr_1", "k1")
    assert order_id_for("usr_1", "k1") != order_id_for("usr_1", "k2")
    assert order_id_for("usr_1", "k1") != order_id_for("usr_2", "k1")
    assert order_id_for("usr_1", "k1").startswith("ord_")


async def test_stale_key_takeover_lands_on_the_same_order(make_snapshot, saga):
    """THE duplicate-order test. Placement's first attempt dies with the key
    reserved but no order (Temporal unreachable). Five minutes later the
    customer retries the same key: the store's stale-IN_PROGRESS takeover
    hands the key back, placement runs again — and must converge on the
    same order id, so the second run adopts the first one's row instead of
    creating a second dinner."""
    service, sessions, saga = await _service(make_snapshot, saga)

    saga.fail_place = SagaUnavailable("temporal unreachable")
    with pytest.raises(SagaUnavailable):
        await _place(service)
    async with sessions() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one() == 0
        # The key is NOT released: a fresh key would mean a fresh order id.
        assert (await s.execute(sa.select(idempotency_keys.c.status))).scalar_one() == "IN_PROGRESS"

    # Age the reservation past the takeover TTL (300s) — what waiting does.
    async with sessions() as s:
        await s.execute(
            idempotency_keys.update().values(
                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=3600)
            )
        )
        await s.commit()

    saga.fail_place = None
    retry = await _place(service)
    assert isinstance(retry, Placed)
    async with sessions() as s:
        ids = (await s.execute(sa.select(orders.c.order_id))).scalars().all()
    assert ids == [order_id_for("usr_1", "k1")]  # ONE order, the derived id


async def test_slow_worker_answers_pending_without_a_row(make_snapshot, saga):
    """The await budget expired but the workflow is durable: 202-shaped
    outcome, no order row YET, key still IN_PROGRESS (so an immediate retry
    waits rather than forking)."""
    service, sessions, saga = await _service(make_snapshot, saga)
    saga.pending = True

    outcome = await _place(service)
    assert outcome == PlacementPending(order_id_for("usr_1", "k1"))
    async with sessions() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one() == 0
        assert (await s.execute(sa.select(idempotency_keys.c.status))).scalar_one() == "IN_PROGRESS"


async def test_closed_saga_adopts_the_order_it_already_made(make_snapshot, saga):
    """A key reused past its 24h replay TTL derives the id of an order that
    already ran to completion. REJECT_DUPLICATE refuses the start; the
    honest answer is that finished order, with its REAL status."""
    service, sessions, saga = await _service(make_snapshot, saga)
    first = await _place(service)
    async with sessions() as s:
        await s.execute(orders.update().values(status="SETTLED"))
        await s.execute(idempotency_keys.delete())  # replay TTL expired
        await s.commit()

    saga.fail_place = SagaClosed(first.order_id)
    outcome = await _place(service)
    assert outcome == Placed(order_id=first.order_id, status="SETTLED")


async def test_closed_saga_with_no_order_is_an_outage_not_an_answer(make_snapshot, saga):
    """Same refusal, but nothing was ever written under that id — there is
    no truthful 202 to give, so it surfaces as a 503-shaped failure."""
    service, sessions, saga = await _service(make_snapshot, saga)
    saga.fail_place = SagaClosed("ord_missing")
    with pytest.raises(SagaUnavailable):
        await _place(service)
