"""Domain-level placement: the DB artifacts HTTP can't see — the
four-writes-one-transaction guarantee, snapshot contents, event identity."""

import sqlalchemy as sa
from order.db import idempotency_keys, metadata, order_items, orders, outbox
from order.domain.service import OrderService, Placed
from smartfood_idempotency import IdempotencyStore, body_hash
from smartfood_outbox import event_id
from smartfood_pricing import Line, PricingConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Deliberately distinct from conftest's fakes — these carry DIFFERENT
# contracts (static canned answers; a saga that refuses every signal),
# so they carry different names.


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


class NeverSignalsSaga:
    def __init__(self):
        self.started: list[str] = []

    async def start(self, order_id):
        self.started.append(order_id)
        return True

    async def signal_decision(self, order_id, verdict):
        raise AssertionError("placement never signals")  # pragma: no cover

    async def signal_food_ready(self, order_id):
        raise AssertionError("placement never signals")  # pragma: no cover

    async def signal_cancel(self, order_id):
        raise AssertionError("placement never signals")  # pragma: no cover


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


async def _service(make_snapshot):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    saga = NeverSignalsSaga()
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


async def test_placement_writes_all_four_artifacts_in_one_commit(make_snapshot):
    service, sessions, saga = await _service(make_snapshot)
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

    assert saga.started == [order.order_id]


async def test_replay_never_touches_the_database_again(make_snapshot):
    service, sessions, saga = await _service(make_snapshot)
    first = await _place(service)
    replay = await _place(service)  # same key, same hash
    from smartfood_idempotency import Replay

    assert isinstance(replay, Replay)
    assert replay.response_body["order_id"] == first.order_id
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one()
    assert count == 1
    assert len(saga.started) == 1


async def test_saga_outage_still_answers_202_and_the_sweeper_heals(make_snapshot):
    """The commit→start gap, non-crash flavor: Temporal is down when the
    post-commit start runs. The order is committed with a stored 202 —
    a customer-facing 500 would contradict both and invite a duplicate
    re-order. place() answers the committed truth; the sweeper pays the
    debt once Temporal returns."""
    from order.sweeper import Sweeper

    service, sessions, saga = await _service(make_snapshot)

    async def exploding_start(order_id):
        raise RuntimeError("temporal unreachable")

    saga.start = exploding_start
    outcome = await _place(service)
    assert isinstance(outcome, Placed)  # the 202 body — not an INTERNAL 500
    async with sessions() as s:
        status = (await s.execute(sa.select(orders.c.status))).scalar_one()
    assert status == "PLACED"

    healer = NeverSignalsSaga()
    swept = await Sweeper(sessions, healer, min_age_seconds=0).sweep_once()
    assert swept == 1
    assert healer.started == [outcome.order_id]
