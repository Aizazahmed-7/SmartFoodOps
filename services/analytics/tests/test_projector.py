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


async def test_fold_guarantees_one_row_per_order():
    """The invariant that makes the bulk upsert LEGAL: Postgres refuses one
    statement whose ON CONFLICT DO UPDATE touches a row twice, and a single
    poll routinely carries an order's whole lifecycle. sqlite tolerates the
    duplicate, so the behavioral suite alone could never catch a broken
    fold — this test pins the seam directly."""
    from analytics.consumers import fold_facts

    rows = fold_facts(
        [
            _event("OrderPlaced", at="2026-08-24T10:00:00+00:00"),
            _event("OrderConfirmed", at="2026-08-24T10:00:02+00:00"),
            _event("OrderPlaced", order_id="ord_2"),
            _event("OrderRefundLaunched"),  # unknown type: contributes nothing
        ]
    )
    assert sorted(r["order_id"] for r in rows) == ["ord_1", "ord_2"]
    merged = next(r for r in rows if r["order_id"] == "ord_1")
    assert merged["status"] == "CONFIRMED"  # later event wins shared columns
    assert "placed_at" in merged and "confirmed_at" in merged  # both milestones kept


async def test_mixed_shapes_in_one_batch_group_by_signature():
    """Two orders whose merged rows carry DIFFERENT column sets (one has
    cancel_reason, one does not) must both land — the repo groups them into
    one statement per signature; a naive single statement cannot express
    two SET lists."""
    sessions = await _sessions()
    await FactsProjector(sessions).handle_batch(
        [
            _event("OrderPlaced", order_id="ord_a"),
            _event("OrderPlaced", order_id="ord_b"),
            _event("OrderCancelled", order_id="ord_b", cancel_reason="customer_cancelled"),
        ]
    )
    row_a, row_b = await _row(sessions, "ord_a"), await _row(sessions, "ord_b")
    assert row_a is not None and row_a.status == "PLACED" and row_a.cancel_reason is None
    assert row_b is not None and row_b.status == "CANCELLED"
    assert row_b.cancel_reason == "customer_cancelled"


# ── the views projector (S8) ────────────────────────────────────────


def _view(event_id, user: str | None = "usr_1", restaurant="rst_1", at="2026-08-24T10:00:00+00:00"):
    import json as _json

    return {
        "event_type": "MenuViewed",
        "event_id": event_id,
        "payload": _json.dumps({"restaurant_id": restaurant, "user_id": user, "viewed_at": at}),
    }


async def test_duplicate_view_ids_within_one_batch_land_once():
    """Intra-STATEMENT duplicates: the whole batch is one multi-VALUES
    INSERT .. DO NOTHING, and DO NOTHING (unlike DO UPDATE) legally skips
    a key it already touched — a double-polled hint costs nothing."""
    from analytics.consumers import ViewsProjector
    from analytics.db import menu_views

    sessions = await _sessions()
    await ViewsProjector(sessions).handle_batch([_view("v_dup"), _view("v_dup")])
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(menu_views))).scalar_one()
    assert count == 1


async def test_views_fold_and_redelivery_collapses_on_the_pk():
    from analytics.consumers import ViewsProjector
    from analytics.db import menu_views

    sessions = await _sessions()
    projector = ViewsProjector(sessions)
    batch = [_view("v1"), _view("v2", user=None)]
    await projector.handle_batch(batch)
    await projector.handle_batch(batch)  # redelivery: same view_ids → no-ops
    async with sessions() as s:
        rows = (await s.execute(sa.select(menu_views).order_by(menu_views.c.view_id))).all()
    assert len(rows) == 2
    assert rows[0].user_id == "usr_1" and rows[1].user_id is None


async def test_views_loop_skips_foreign_event_types_and_handles_singles():
    from analytics.consumers import ViewsProjector
    from analytics.db import menu_views

    sessions = await _sessions()
    projector = ViewsProjector(sessions)
    await projector.handle({"event_type": "SomethingElse", "event_id": "x", "payload": "{}"})
    await projector.handle(_view("v9"))
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(menu_views))).scalar_one()
    assert count == 1


async def test_rider_attribution_folds_and_never_blanks():
    """Dispatch milestone: delivered/settled events carry rider_id; an
    out-of-order pre-assignment event (rider null) must not blank it."""
    sessions = await _sessions()
    projector = FactsProjector(sessions)
    await projector.handle_batch([_event("OrderDelivered", status="DELIVERED", rider_id="r_7")])
    await projector.handle_batch(
        [_event("OrderConfirmed", status="CONFIRMED", rider_id=None)]  # late arrival
    )
    row = await _row(sessions)
    assert row.rider_id == "r_7"  # survived the null-carrying replay


async def test_brand_id_folds_and_never_blanks(  # ADR-0028
):
    """A branded event stamps brand_id; a later legacy replay (no brand key)
    must NOT blank it — same convergence rule as rider_id."""
    sessions = await _sessions()
    projector = FactsProjector(sessions)
    await projector.handle(_event("OrderPlaced", brand_id="brd_1"))
    row = await _row(sessions)
    assert row is not None and row.brand_id == "brd_1"

    await projector.handle(_event("OrderConfirmed"))  # legacy shape, no brand_id
    row = await _row(sessions)
    assert row is not None
    assert (row.status, row.brand_id) == ("CONFIRMED", "brd_1")  # folded, not blanked


async def test_brand_repoint_heals_only_null_rows():
    from analytics.consumers import BrandRepointHandler

    sessions = await _sessions()
    projector = FactsProjector(sessions)
    await projector.handle(_event("OrderPlaced", order_id="ord_legacy"))  # brand NULL
    await projector.handle(_event("OrderPlaced", order_id="ord_new", brand_id="brd_1"))
    await projector.handle(
        _event("OrderPlaced", order_id="ord_other", restaurant_id="rst_2")
    )  # a different branch

    import json as _json

    handler = BrandRepointHandler(sessions)
    catalog_event = {
        "aggregate_type": "restaurant",
        "aggregate_id": "rst_1",
        "payload": _json.dumps({"owner_user_id": "u", "brand_id": "brd_1"}),
    }
    await handler.handle(catalog_event)
    await handler.handle(catalog_event)  # replay — naturally idempotent

    async with sessions() as s:
        rows = {r.order_id: r.brand_id for r in (await s.execute(sa.select(order_facts))).all()}
    assert rows == {"ord_legacy": "brd_1", "ord_new": "brd_1", "ord_other": None}

    # brand aggregates, brandless and foreign-type payloads: ignored
    await handler.handle(
        {
            "aggregate_type": "restaurant",
            "aggregate_id": "brd_1",
            "payload": _json.dumps({"brand_id": "brd_1"}),
        }
    )
    await handler.handle(
        {"aggregate_type": "restaurant", "aggregate_id": "rst_2", "payload": _json.dumps({})}
    )
    await handler.handle(
        {
            "aggregate_type": "stock",
            "aggregate_id": "rst_2",
            "payload": _json.dumps({"brand_id": "brd_9"}),
        }
    )
    async with sessions() as s:
        untouched = (
            await s.execute(
                sa.select(order_facts.c.brand_id).where(order_facts.c.order_id == "ord_other")
            )
        ).scalar_one()
    assert untouched is None
