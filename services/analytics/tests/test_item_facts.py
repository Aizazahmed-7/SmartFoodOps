"""Item-level order facts (FR-96).

`order_facts` carries totals and no item ids, so this is the table
recommendation acceptance and taste profiles are built from. It is written
once from OrderPlaced and never updated — which makes redelivery the whole
correctness story, and a fold that double-counts the one way to get it
wrong.
"""

import json

import sqlalchemy as sa
from analytics.adapters.repo import item_values
from analytics.consumers import ItemFactsProjector
from analytics.db import metadata, order_item_facts
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PLACED_AT = "2026-09-22T10:00:00+00:00"


async def _sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _line(item_id: str | None = "itm_karahi", name="Chicken Karahi", qty=1, total=1200, **over):
    line = {
        "menu_item_id": item_id,
        "name": name,
        "unit_price_cents": total // max(qty, 1),
        "qty": qty,
        "options": [],
        "line_total_cents": total,
    }
    line.update(over)
    return line


def _placed(order_id="ord_1", *lines, event_type="OrderPlaced", **over):
    payload = {
        "order_id": order_id,
        "user_id": "usr_1",
        "restaurant_id": "rst_1",
        "brand_id": "brd_1",
        "status": "PLACED",
        "items": list(lines) or [_line()],
        "placed_at": PLACED_AT,
        **over,
    }
    return {"event_type": event_type, "payload": json.dumps(payload)}


async def _rows(sessions):
    async with sessions() as session:
        result = await session.execute(
            sa.select(order_item_facts).order_by(order_item_facts.c.menu_item_id)
        )
        return [dict(r._mapping) for r in result]  # noqa: SLF001


# ── the fold ────────────────────────────────────────────────────────


def test_one_row_per_dish_with_the_facts_a_recommender_needs():
    (row,) = item_values(json.loads(_placed("ord_1", _line())["payload"]))
    assert row["order_id"] == "ord_1" and row["menu_item_id"] == "itm_karahi"
    assert row["user_id"] == "usr_1" and row["restaurant_id"] == "rst_1"
    assert row["brand_id"] == "brd_1"
    assert row["qty"] == 1 and row["line_total_cents"] == 1200


def test_the_same_dish_on_two_lines_is_one_row_with_the_quantities_summed():
    """The cart splits a line per option combination, so "large, no chilli"
    and "small" are two lines and one dish. Keying on the pair without
    summing would silently drop one; keying per line would make the row a
    line number, which is not a fact anybody joins on."""
    rows = item_values(
        json.loads(
            _placed(
                "ord_1",
                _line(qty=1, total=1200),
                _line(qty=2, total=2000),
            )["payload"]
        )
    )
    assert len(rows) == 1
    assert rows[0]["qty"] == 3 and rows[0]["line_total_cents"] == 3200


def test_the_name_is_snapshotted_as_ordered():
    """A menu edit must not rewrite history. The recommender keys on ids —
    this column is for whoever reads the table."""
    (row,) = item_values(json.loads(_placed("ord_1", _line(name="Chicken Karahi"))["payload"]))
    assert row["name_snapshot"] == "Chicken Karahi"


def test_a_line_with_no_item_id_is_skipped_not_fatal():
    """A row without one is unusable to a recommender that keys on ids, and
    parking the whole topic over a malformed line would take the order facts
    down with it."""
    rows = item_values(
        json.loads(_placed("ord_1", _line(), _line(item_id=None, name="mystery"))["payload"])
    )
    assert [r["menu_item_id"] for r in rows] == ["itm_karahi"]


def test_an_order_with_no_items_produces_nothing():
    assert (
        item_values(json.loads(_placed("ord_1", event_type="OrderPlaced", items=[])["payload"]))
        == []
    )


# ── redelivery ──────────────────────────────────────────────────────


async def test_a_redelivered_order_does_not_double_count():
    """At-least-once is the contract, and these rows feed a COUNT. An item
    fact is written once and never changes, so a redelivery has nothing to
    update and must not pretend otherwise."""
    sessions = await _sessions()
    projector = ItemFactsProjector(sessions)
    await projector.handle_batch([_placed("ord_1", _line(qty=2, total=2400))])
    await projector.handle_batch([_placed("ord_1", _line(qty=2, total=2400))])
    rows = await _rows(sessions)
    assert len(rows) == 1 and rows[0]["qty"] == 2


async def test_one_batch_carrying_the_same_order_twice_is_legal():
    """A batch spanning a replayed partition routinely has duplicate keys
    inside one statement — DO NOTHING tolerates that, DO UPDATE would not."""
    sessions = await _sessions()
    await ItemFactsProjector(sessions).handle_batch(
        [_placed("ord_1", _line()), _placed("ord_1", _line())]
    )
    assert len(await _rows(sessions)) == 1


# ── what it ignores ─────────────────────────────────────────────────


async def test_only_OrderPlaced_carries_items():
    """Forward compatibility on a shared topic: every other lifecycle event
    skips, the same rule the views projector follows."""
    sessions = await _sessions()
    await ItemFactsProjector(sessions).handle_batch(
        [
            _placed("ord_1", _line()),
            _placed("ord_2", _line(), event_type="OrderConfirmed"),
            _placed("ord_3", _line(), event_type="SomethingFromTheFuture"),
        ]
    )
    assert [r["order_id"] for r in await _rows(sessions)] == ["ord_1"]


async def test_a_cancelled_orders_items_stay_facts():
    """The lifecycle lives on `order_facts` and is joined to. A cancelled
    order's items are still facts about what was placed, and a second
    `status` here would be a second writer for a column that has one."""
    sessions = await _sessions()
    projector = ItemFactsProjector(sessions)
    await projector.handle_batch([_placed("ord_1", _line())])
    await projector.handle_batch([_placed("ord_1", _line(), event_type="OrderCancelled")])
    assert len(await _rows(sessions)) == 1


async def test_a_batch_of_one_goes_through_the_same_fold():
    """The degrade-to-singles pass rides `handle`; it must not be a second
    code path."""
    sessions = await _sessions()
    await ItemFactsProjector(sessions).handle(_placed("ord_1", _line()))
    assert len(await _rows(sessions)) == 1
