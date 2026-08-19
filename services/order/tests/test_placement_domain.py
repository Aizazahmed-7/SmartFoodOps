"""Domain-level placement: the DB artifacts HTTP can't see — the
one-transaction guarantee, snapshot contents, event identity, and the
ADR-0024 idempotency story (the orders row IS the record)."""

import pytest
import sqlalchemy as sa
from order.db import metadata, order_items, orders, outbox
from order.domain.ports import PlacementPending, SagaClosed, SagaUnavailable
from order.domain.service import (
    HashMismatch,
    OrderService,
    Placed,
    Replayed,
    order_id_for,
)
from order.values import PlacementAck
from smartfood_idempotency import body_hash
from smartfood_outbox import event_id
from smartfood_pricing import Line, MenuVersionChanged, PricingConfig
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


async def _place(service, key="k1", body=b"the-body", menu_version=3):
    return await service.place(
        user_id="usr_1",
        idem_key=key,
        request_hash=body_hash(body),
        restaurant_id="rst_1",
        menu_version=menu_version,
        lines=_lines(),
        address_id="adr_1",
        card_token="tok_ok",
    )


async def test_placement_writes_row_lines_and_event_in_one_commit(make_snapshot, saga):
    """Unchanged guarantee, new author: the three writes happen inside the
    saga's create_order activity and commit together — and the row carries
    request_hash, because the row IS the idempotency record (ADR-0024)."""
    service, sessions, saga = await _service(make_snapshot, saga)
    outcome = await _place(service)
    assert isinstance(outcome, Placed)

    async with sessions() as s:
        order = (await s.execute(sa.select(orders))).one()
        items = (await s.execute(sa.select(order_items))).all()
        event = (await s.execute(sa.select(outbox))).one()

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

    # 4. the row is the idempotency record: the body it answers for
    assert order.request_hash == body_hash(b"the-body")

    assert saga.placed == [order.order_id]
    # The id is DERIVED from the key, not random — this is the property the
    # whole retry story rests on (ADR-0023/0024).
    assert order.order_id == order_id_for("usr_1", "k1")


async def test_replay_answers_from_the_row_before_anything_else(make_snapshot, saga):
    """Same key again → the row-read answers. The saga is not asked twice,
    and — because the read runs BEFORE pricing — a replay can never be
    re-priced into a refusal while a kitchen cooks the order."""
    service, sessions, saga = await _service(make_snapshot, saga)
    first = await _place(service)
    replay = await _place(service)  # same key, same hash

    assert replay == Replayed(order_id=first.order_id, status="PLACED")
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one()
    assert count == 1
    assert len(saga.placed) == 1  # the saga was not asked a second time


async def test_replay_reports_the_current_status_not_a_frozen_copy(make_snapshot, saga):
    """The row moved on (the saga confirmed it) — the replay says so.
    Truth over a stored snapshot: the client polls this status anyway."""
    service, sessions, saga = await _service(make_snapshot, saga)
    first = await _place(service)
    async with sessions() as s:
        await s.execute(orders.update().values(status="CONFIRMED"))
        await s.commit()

    assert await _place(service) == Replayed(order_id=first.order_id, status="CONFIRMED")


async def test_key_reused_for_a_different_cart_is_a_client_bug(make_snapshot, saga):
    """Same key, different body: answering with the OLD order would hand
    the client food they did not ask for, silently. The request_hash on
    the row catches it — 422, loudly."""
    service, _, saga = await _service(make_snapshot, saga)
    await _place(service)
    assert isinstance(await _place(service, body=b"a-DIFFERENT-cart"), HashMismatch)


async def test_rows_born_before_the_hash_column_still_replay(make_snapshot, saga):
    """Migration 0004 left old rows with request_hash NULL — they skip the
    mismatch guard rather than 422 every replay of a pre-deploy order."""
    service, sessions, saga = await _service(make_snapshot, saga)
    first = await _place(service)
    async with sessions() as s:
        await s.execute(orders.update().values(request_hash=None))
        await s.commit()

    assert await _place(service, body=b"whatever") == Replayed(
        order_id=first.order_id, status="PLACED"
    )


async def test_order_ids_are_derived_from_the_key_not_random():
    """Same scope + key → same id, always; a different key → a different
    order. Randomness here would mint a second order on every takeover."""
    assert order_id_for("usr_1", "k1") == order_id_for("usr_1", "k1")
    assert order_id_for("usr_1", "k1") != order_id_for("usr_1", "k2")
    assert order_id_for("usr_1", "k1") != order_id_for("usr_2", "k1")
    assert order_id_for("usr_1", "k1").startswith("ord_")


async def test_temporal_outage_leaves_nothing_behind_and_the_retry_converges(make_snapshot, saga):
    """THE duplicate-order test, ADR-0024 edition. A Temporal outage during
    placement writes NOTHING — no row, no lock — so there is nothing to
    take over and nothing to garbage-collect. The retry simply re-derives
    the same id and runs again: one order, ever."""
    service, sessions, saga = await _service(make_snapshot, saga)

    saga.fail_place = SagaUnavailable("temporal unreachable")
    with pytest.raises(SagaUnavailable):
        await _place(service)
    async with sessions() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one() == 0

    saga.fail_place = None
    retry = await _place(service)
    assert isinstance(retry, Placed)
    async with sessions() as s:
        ids = (await s.execute(sa.select(orders.c.order_id))).scalars().all()
    assert ids == [order_id_for("usr_1", "k1")]  # ONE order, the derived id


async def test_slow_worker_answers_pending_without_a_row(make_snapshot, saga):
    """The await budget expired but the workflow is durable: 202-shaped
    outcome, no order row YET — a retry in this window attaches to the
    running workflow via the same derived id."""
    service, sessions, saga = await _service(make_snapshot, saga)
    saga.pending = True

    outcome = await _place(service)
    assert outcome == PlacementPending(order_id_for("usr_1", "k1"))
    async with sessions() as s:
        assert (await s.execute(sa.select(sa.func.count()).select_from(orders))).scalar_one() == 0


async def test_refusal_loses_to_a_workflow_already_making_the_order(make_snapshot, saga):
    """The pending-window race (ADR-0024 amendment): a retry lands while
    the workflow is durably in flight, the row is not visible, and the
    menu has drifted. Re-pricing refuses — but the refusal must LOSE to
    the running workflow's ack, or "re-confirm your cart" would mint a
    second order for one dinner."""
    service, _, saga = await _service(make_snapshot, saga)
    order_id = order_id_for("usr_1", "k1")
    saga.attach_ack = PlacementAck(order_id=order_id, status="PLACED")

    # menu_version=99 vs snapshot version 3 → MenuVersionChanged, refused…
    outcome = await _place(service, menu_version=99)
    # …but the attach probe found the in-flight placement: its ack wins.
    assert outcome == Placed(order_id=order_id, status="PLACED")
    assert saga.attaches == [order_id]


async def test_refusal_during_pending_window_can_answer_pending(make_snapshot, saga):
    """Same race, but create_order has not committed inside the attach
    budget either: the probe answers pending, and pending still beats
    telling the customer to re-confirm."""
    service, _, saga = await _service(make_snapshot, saga)
    order_id = order_id_for("usr_1", "k1")
    saga.attach_ack = PlacementPending(order_id)

    assert await _place(service, menu_version=99) == PlacementPending(order_id)


async def test_refusal_stands_when_no_workflow_is_running(make_snapshot, saga):
    """The common case: a genuinely stale cart, no workflow anywhere. The
    attach probe answers SagaGone and the 409 reaches the client — that IS
    the honest answer (re-quote, re-confirm)."""
    service, _, saga = await _service(make_snapshot, saga)

    with pytest.raises(MenuVersionChanged):
        await _place(service, menu_version=99)
    assert saga.attaches == [order_id_for("usr_1", "k1")]  # probed, found nothing


async def test_closed_saga_adopts_the_order_it_already_made(make_snapshot, saga):
    """The read→start race: between place()'s row-read (empty) and its
    start RPC, create_order committed AND the workflow ran to a close —
    REJECT_DUPLICATE refuses. The honest answer is the order that raced
    us, with its REAL status."""
    service, _, saga = await _service(make_snapshot, saga)
    order_id = order_id_for("usr_1", "k1")
    saga.fail_place_after_create = SagaClosed(order_id)

    outcome = await _place(service)
    assert outcome == Placed(order_id=order_id, status="PLACED")


async def test_closed_saga_with_no_order_is_an_outage_not_an_answer(make_snapshot, saga):
    """Same refusal, but nothing was ever written under that id — there is
    no truthful 202 to give, so it surfaces as a 503-shaped failure."""
    service, sessions, saga = await _service(make_snapshot, saga)
    saga.fail_place = SagaClosed("ord_missing")
    with pytest.raises(SagaUnavailable):
        await _place(service)
