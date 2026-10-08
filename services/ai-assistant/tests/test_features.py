"""Order features and popularity (FR-80).

Facts in, aggregates out — computed at read time by grouping, never stored
as counters, because a counter cannot absorb at-least-once redelivery.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from ai_assistant.adapters.features import FeatureRepo
from ai_assistant.consumers import FeatureHandler, order_rows
from ai_assistant.db import metadata, restaurant_chunks
from ai_assistant.domain.popularity import BAND, WINDOW, Popular, hours_around, rank
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOON = datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
VERSION = "fake:512"


# ── the pure rules ──────────────────────────────────────────────────


def test_the_band_wraps_at_midnight():
    """23:00 and 00:00 are the same late-night eating occasion. Without
    wrapping they would see disjoint windows, and the hour with the least
    data would get the least help."""
    assert hours_around(NOON.replace(hour=23)) == [0, 22, 23]
    assert hours_around(NOON.replace(hour=0)) == [0, 1, 23]


def test_the_band_is_one_meal_wide():
    assert len(hours_around(NOON)) == 2 * BAND + 1


def test_ranking_counts_people_not_portions():
    """Units would let one catering order of forty naans outvote a dish
    twenty people chose — the opposite of what a recommendation is for."""
    ordered = rank([Popular("bulk", 1, 40), Popular("loved", 20, 20)], 2)
    assert [p.item_id for p in ordered] == ["loved", "bulk"]


def test_units_break_a_tie():
    ordered = rank([Popular("singly", 5, 5), Popular("in_twos", 5, 10)], 2)
    assert [p.item_id for p in ordered] == ["in_twos", "singly"]


def test_the_id_breaks_the_last_tie():
    """A recommendation that reshuffles between two identical requests looks
    broken and cannot be debugged."""
    ordered = rank([Popular("b", 1, 1), Popular("a", 1, 1)], 2)
    assert [p.item_id for p in ordered] == ["a", "b"]


# ── the projection ──────────────────────────────────────────────────


def _line(item_id="itm_karahi", qty=1):
    return {"menu_item_id": item_id, "name": "Chicken Karahi", "qty": qty, "options": []}


def _placed(order_id="ord_1", *lines, at=NOON, user="usr_1", restaurant="rst_1", **over):
    payload = {
        "order_id": order_id,
        "user_id": user,
        "restaurant_id": restaurant,
        "items": list(lines) or [_line()],
        "placed_at": at.isoformat(),
        **over,
    }
    return {"event_type": "OrderPlaced", "payload": json.dumps(payload)}


def test_lines_sharing_a_dish_are_summed():
    """The cart splits a line per option combination, so one order carries
    the same dish twice — summing keeps the row an absolute value."""
    (row,) = order_rows(json.loads(_placed("ord_1", _line(qty=1), _line(qty=2))["payload"]))
    assert row["qty"] == 3


def test_a_line_with_no_item_id_is_skipped_not_fatal():
    rows = order_rows(json.loads(_placed("ord_1", _line(), {"qty": 1})["payload"]))
    assert [r["item_id"] for r in rows] == ["itm_karahi"]


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        for restaurant, city in (
            ("rst_1", "springfield"),
            ("rst_2", "springfield"),
            ("rst_9", "karachi"),
        ):
            await session.execute(
                restaurant_chunks.insert().values(
                    id=f"{restaurant}:self",
                    model_version=VERSION,
                    restaurant_id=restaurant,
                    city=city,
                    brand_id=None,
                    cuisines=[],
                    status="open",
                    content=restaurant,
                    content_hash="h",
                    embedding=[0.0] * 512,
                    updated_at=NOON,
                )
            )
        await session.commit()
    yield maker
    await engine.dispose()


async def _popular(sessions, at=NOON, city="springfield"):
    async with sessions() as session:
        return rank(
            await FeatureRepo(session).popular_in(city=city, at=at, model_version=VERSION), 10
        )


async def test_a_redelivered_order_does_not_double_count(sessions):
    """These rows feed a COUNT, and at-least-once is the contract."""
    handler = FeatureHandler(sessions)
    await handler.handle_batch([_placed("ord_1", _line(qty=2))])
    await handler.handle_batch([_placed("ord_1", _line(qty=2))])
    (top,) = await _popular(sessions)
    assert top.orders == 1 and top.units == 2


async def test_only_OrderPlaced_carries_items(sessions):
    handler = FeatureHandler(sessions)
    await handler.handle_batch(
        [_placed("ord_1"), {"event_type": "OrderDelivered", "payload": "{}"}]
    )
    assert len(await _popular(sessions)) == 1


async def test_a_batch_of_one_takes_the_same_path(sessions):
    await FeatureHandler(sessions).handle(_placed("ord_1"))
    assert len(await _popular(sessions)) == 1


# ── the read ────────────────────────────────────────────────────────


async def test_popularity_is_scoped_to_the_city(sessions):
    """The city mapping comes from our own index, so "city" means exactly
    what it means in retrieval — the restaurant's city, not the delivery
    address's. Mixing the two would make a recommendation disagree with the
    search results beside it."""
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("ord_1", _line("itm_local"), restaurant="rst_1"),
            _placed("ord_2", _line("itm_elsewhere"), restaurant="rst_9"),
        ]
    )
    assert [p.item_id for p in await _popular(sessions)] == ["itm_local"]
    assert [p.item_id for p in await _popular(sessions, city="karachi")] == ["itm_elsewhere"]


async def test_a_dish_sold_by_two_branches_is_not_counted_twice(sessions):
    """Scoped by RESTAURANT, not by item: the index carries one chunk per
    (restaurant, item), so joining on items would let a widely-franchised
    base dish outrank everything by arithmetic."""
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("ord_1", _line("itm_base"), restaurant="rst_1"),
            _placed("ord_2", _line("itm_base"), restaurant="rst_2"),
        ]
    )
    (top,) = await _popular(sessions)
    assert top.item_id == "itm_base" and top.orders == 2  # two orders, not four


async def test_orders_outside_the_hour_band_do_not_count(sessions):
    """Wider and breakfast recommends biryani; this is what keeps "around
    now" meaning something."""
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("ord_1", _line("itm_lunch"), at=NOON),
            _placed("ord_2", _line("itm_breakfast"), at=NOON.replace(hour=7)),
        ]
    )
    assert [p.item_id for p in await _popular(sessions)] == ["itm_lunch"]


async def test_orders_older_than_the_window_do_not_count(sessions):
    """The bound on how wrong this can be: nothing here notices a menu
    change, so the worst case is one window of recommending a dish that
    stopped existing."""
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("ord_1", _line("itm_recent"), at=NOON - timedelta(days=1)),
            _placed("ord_2", _line("itm_ancient"), at=NOON - WINDOW - timedelta(days=1)),
        ]
    )
    assert [p.item_id for p in await _popular(sessions)] == ["itm_recent"]


async def test_a_city_with_no_orders_returns_nothing(sessions):
    """Popularity is a fallback, not a guarantee — the graph falls through
    to the apology rather than inventing a history."""
    assert await _popular(sessions) == []


# ── hydration: ids become passages ──────────────────────────────────


async def _seed_menu(sessions, *items):
    from ai_assistant.db import item_chunks

    async with sessions() as session:
        for item_id, restaurant, text in items:
            await session.execute(
                item_chunks.insert().values(
                    id=f"{restaurant}:{item_id}",
                    model_version=VERSION,
                    restaurant_id=restaurant,
                    item_id=item_id,
                    # FR-88 gave the index a name column; irrelevant here,
                    # but every dish has one.
                    name=f"Dish {item_id}",
                    city="springfield",
                    category="Mains",
                    content=text,
                    content_hash="h",
                    embedding=[0.0] * 512,
                    tags=[],
                    cuisines=[],
                    price_cents=100,
                    available=True,
                    status="open",
                    updated_at=NOON,
                )
            )
        await session.commit()


async def test_popular_ids_become_passages_with_menu_text(sessions):
    from ai_assistant.recommend import Recommender

    await _seed_menu(sessions, ("itm_karahi", "rst_1", "Chicken Karahi\nWok-cooked."))
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", _line("itm_karahi"))])

    (passage,) = await Recommender(sessions, model_version=VERSION).popular(
        city="springfield", limit=5, at=NOON
    )
    assert passage.item_id == "itm_karahi" and passage.restaurant_id == "rst_1"
    assert "Chicken Karahi" in passage.text


async def test_a_dish_no_longer_on_the_menu_is_not_recommended(sessions):
    """Hydrated from the INDEX, not the order history: the history knows a
    name as it was sold, the index knows what this service is willing to
    show. A dish taken off the menu drops out rather than being recommended
    from a name nobody can order."""
    from ai_assistant.recommend import Recommender

    await _seed_menu(sessions, ("itm_still_here", "rst_1", "Raita"))
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("ord_1", _line("itm_still_here")),
            _placed("ord_2", _line("itm_delisted")),
        ]
    )
    found = await Recommender(sessions, model_version=VERSION).popular(
        city="springfield", limit=5, at=NOON
    )
    assert [p.item_id for p in found] == ["itm_still_here"]


async def test_no_index_means_no_recommendation(sessions):
    """No index means no text to show, whatever the history says."""
    from ai_assistant.recommend import Recommender

    await FeatureHandler(sessions).handle_batch([_placed("ord_1")])
    assert (
        await Recommender(sessions, model_version=VERSION).popular(
            city="springfield", limit=5, at=NOON
        )
        == []
    )


async def test_a_city_with_history_but_no_matches_recommends_nothing(sessions):
    from ai_assistant.recommend import Recommender

    await _seed_menu(sessions, ("itm_karahi", "rst_1", "Chicken Karahi"))
    assert (
        await Recommender(sessions, model_version=VERSION).popular(city="karachi", limit=5, at=NOON)
        == []
    )


async def test_looking_up_no_items_costs_no_query(sessions):
    from ai_assistant.adapters.vector_store import PostgresVectorStore

    async with sessions() as session:
        store = PostgresVectorStore(session)
        assert await store.texts_by_item(item_ids=[], model_version=VERSION) == {}


async def test_an_empty_batch_costs_no_statement(sessions):
    """Every non-OrderPlaced batch reaches the repo with nothing in it, and
    a lifecycle-only poll is the common case on a shared topic."""
    async with sessions() as session:
        await FeatureRepo(session).record([])
        await session.commit()
    assert await _popular(sessions) == []


def test_the_hour_expression_is_split_by_dialect():
    """sqlite has no EXTRACT and the unit suite runs on sqlite, so without
    the split the time-of-day band would be tested nowhere and shipped
    untested — the shape of every dialect bug this project has had."""
    from ai_assistant.adapters.features import _hour_of
    from ai_assistant.db import order_items

    assert "strftime" in str(_hour_of(order_items.c.placed_at, "sqlite"))
    assert "extract" in str(_hour_of(order_items.c.placed_at, "postgresql")).lower()
