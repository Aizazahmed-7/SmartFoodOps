"""Budgets and combinations (FR-76, FR-77).

FR-76 calls a budget a HARD predicate, so these are mostly tests about the
ways a budget can quietly be soft: a base price that is not the real floor,
an indexed price that is not today's price, and a pair nobody could put in
one cart.
"""

from datetime import UTC, datetime, timedelta

import pytest
from ai_assistant.adapters.features import FeatureRepo
from ai_assistant.consumers import FeatureHandler
from ai_assistant.db import metadata, restaurant_chunks
from ai_assistant.domain.combos import Combo, affordable, combine, minimum_total
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 26, 12, 30, tzinfo=UTC)
VERSION = "fake:512"


def _group(min_select=1, deltas=(0, 300), name="Size"):
    return {
        "id": name.lower(),
        "name": name,
        "min_select": min_select,
        "max_select": 1,
        "options": [{"id": f"o{n}", "price_delta_cents": d} for n, d in enumerate(deltas)],
    }


def _item(price=1000, groups=()):
    return {"id": "itm_1", "price_cents": price, "modifier_groups": list(groups)}


# ── what a dish really costs ────────────────────────────────────────


def test_a_dish_with_no_required_options_costs_its_price():
    assert minimum_total(_item(1000)) == 1000


def test_a_free_required_option_does_not_raise_the_floor():
    """A Size group whose cheapest choice is +0 leaves the floor alone —
    the customer must choose, but choosing costs nothing."""
    assert minimum_total(_item(1000, [_group(deltas=(0, 300))])) == 1000


def test_a_required_option_that_costs_money_raises_the_floor():
    """THE budget bug this exists to prevent. The pricing engine refuses a
    line whose required group is unpicked, so a dish whose cheapest Size is
    +300 can never be bought for its base price — budgeting on `price_cents`
    would promise a total the customer cannot reach."""
    assert minimum_total(_item(1000, [_group(deltas=(300, 500))])) == 1300


def test_an_optional_group_cannot_raise_the_floor():
    """The customer can decline it, so it is not part of the minimum."""
    assert minimum_total(_item(1000, [_group(min_select=0, deltas=(150,), name="Add-ons")])) == 1000


def test_several_required_groups_all_count():
    floor = minimum_total(
        _item(1000, [_group(deltas=(200, 400)), _group(deltas=(50,), name="Base")])
    )
    assert floor == 1250


def test_a_group_requiring_two_picks_counts_the_two_cheapest():
    group = {
        "id": "g",
        "name": "Pick two",
        "min_select": 2,
        "max_select": 2,
        "options": [{"price_delta_cents": d} for d in (100, 200, 900)],
    }
    assert minimum_total(_item(1000, [group])) == 1300


def test_a_required_group_with_no_options_fails_every_budget():
    """A catalog data bug, not a price. Treating it as free would hide a
    dish that can never be ordered inside a budget that looks met."""
    unorderable = _item(1000, [{"id": "g", "name": "Size", "min_select": 1, "options": []}])
    assert minimum_total(unorderable) > 10**8
    assert affordable([{"min_total_cents": minimum_total(unorderable)}], 1_000_000) == []


# ── the filter ──────────────────────────────────────────────────────


def _card(price, floor=None):
    """A CARD, as the route sees one: priced, and carrying no modifier
    groups — which is why the floor has to travel as its own field."""
    return {"item_id": "a", "price_cents": price, "min_total_cents": floor or price}


def test_the_budget_is_measured_against_the_floor_not_the_base():
    """Base 1000, required Size starting at +300. Recomputing the floor from
    a card would find no groups, read 1000, and admit it — which `affordable`
    did on its first outing, passing exactly the dishes FR-76 excludes."""
    looks_cheap = _card(1000, floor=1300)
    assert affordable([looks_cheap], 1200) == []
    assert affordable([looks_cheap], 1300) == [looks_cheap]


def test_the_budget_boundary_is_inclusive():
    """ "Under ten pounds" means ten pounds is allowed — an exclusive bound
    would drop the dish priced exactly at the number the customer said."""
    assert affordable([_card(1000)], 1000) == [_card(1000)]


# ── combinations ────────────────────────────────────────────────────


PAIRS = [("rst_1", "a", "b", 12), ("rst_1", "a", "c", 5), ("rst_1", "b", "c", 5)]
FLOORS = {("rst_1", "a"): 500, ("rst_1", "b"): 700, ("rst_1", "c"): 400}


def test_combos_are_ranked_by_how_often_they_are_bought_together():
    combos = combine(PAIRS, FLOORS, budget_cents=None, limit=5)
    assert combos[0] == Combo(
        restaurant_id="rst_1", item_ids=("a", "b"), total_cents=1200, orders=12
    )


def test_a_tie_breaks_on_price_then_id():
    """The same data must always produce the same list — a suggestion that
    reshuffles between identical requests cannot be debugged."""
    combos = combine(PAIRS, FLOORS, budget_cents=None, limit=5)
    # Both have 5 orders; a+c is 900 and b+c is 1100, so the cheaper leads.
    assert [c.item_ids for c in combos[1:]] == [("a", "c"), ("b", "c")]


def test_a_combo_over_budget_is_dropped_not_trimmed():
    """FR-76 says no returned COMBINATION exceeds the budget. Returning the
    pair and letting the client notice would be the soft version."""
    combos = combine(PAIRS, FLOORS, budget_cents=1000, limit=5)
    assert [c.item_ids for c in combos] == [("a", "c")]  # 900 fits; 1200 and 1100 do not


def test_a_pair_we_could_not_price_is_dropped_not_guessed():
    """A combo is a promise about a total, and half a total is not one."""
    combos = combine([("rst_1", "a", "missing", 99)], FLOORS, budget_cents=None, limit=5)
    assert combos == []


def test_a_pair_from_another_branch_is_dropped_not_repriced():
    """One item id is served by every branch of a brand (ADR-0028), so an
    item-keyed floor priced a dish at whichever branch answered last — and
    produced combos spanning two branches no cart could hold (B4 review)."""
    elsewhere = [("rst_OTHER", "a", "b", 12)]
    assert combine(elsewhere, FLOORS, budget_cents=None, limit=5) == []


def test_the_limit_applies_after_ranking():
    assert len(combine(PAIRS, FLOORS, budget_cents=None, limit=1)) == 1


# ── the co-order signal ─────────────────────────────────────────────


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
            ("rst_1", "islamabad"),
            ("rst_2", "islamabad"),
            ("rst_9", "lahore"),
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
                    updated_at=NOW,
                )
            )
        await session.commit()
    yield maker
    await engine.dispose()


def _placed(order_id, *items, restaurant="rst_1", at=NOW):
    import json

    return {
        "event_type": "OrderPlaced",
        "payload": json.dumps(
            {
                "order_id": order_id,
                "user_id": "usr_1",
                "restaurant_id": restaurant,
                "items": [{"menu_item_id": i, "name": i, "qty": 1} for i in items],
                "placed_at": at.isoformat(),
            }
        ),
    }


async def _pairs(sessions, city="islamabad"):
    async with sessions() as session:
        return await FeatureRepo(session).co_ordered(
            city=city, since=NOW - timedelta(days=30), model_version=VERSION
        )


async def test_dishes_bought_together_become_a_pair(sessions):
    await FeatureHandler(sessions).handle_batch(
        [_placed("ord_1", "itm_biryani", "itm_raita"), _placed("ord_2", "itm_biryani", "itm_raita")]
    )
    assert await _pairs(sessions) == [("rst_1", "itm_biryani", "itm_raita", 2)]


async def test_a_dish_is_not_paired_with_itself(sessions):
    """A self-join produces the self-pair unless told not to."""
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_solo")])
    assert await _pairs(sessions) == []


async def test_a_pair_appears_once_not_twice(sessions):
    """`a < b` makes each pair canonical — otherwise every combo would be
    suggested in both orders and fill the list with duplicates."""
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_b", "itm_a")])
    assert len(await _pairs(sessions)) == 1


async def test_dishes_from_two_restaurants_are_never_a_combo(sessions):
    """The cart holds one restaurant per order, so a pair spanning two
    kitchens is a suggestion the customer cannot act on (FR-77)."""
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("ord_1", "itm_x", restaurant="rst_1"),
            _placed("ord_1", "itm_y", restaurant="rst_2"),
        ]
    )
    assert await _pairs(sessions) == []


async def test_pairs_are_scoped_to_the_city(sessions):
    await FeatureHandler(sessions).handle_batch(
        [_placed("ord_1", "itm_a", "itm_b", restaurant="rst_9")]
    )
    assert await _pairs(sessions) == []
    assert len(await _pairs(sessions, city="lahore")) == 1


async def test_old_orders_do_not_suggest_combos(sessions):
    await FeatureHandler(sessions).handle_batch(
        [_placed("ord_1", "itm_a", "itm_b", at=NOW - timedelta(days=90))]
    )
    assert await _pairs(sessions) == []


# ── the service seam ────────────────────────────────────────────────


async def test_the_recommender_returns_pairs_unpriced(sessions):
    """Unpriced on purpose: the caller is the only one that can price them,
    and pricing is what decides which pairs survive a budget."""
    from ai_assistant.adapters.repo import IndexStateRepo
    from ai_assistant.recommend import Recommender

    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version=VERSION, now=NOW)
        await session.commit()
    await FeatureHandler(sessions).handle_batch(
        [_placed("ord_1", "itm_a", "itm_b"), _placed("ord_2", "itm_a", "itm_b")]
    )

    assert await Recommender(sessions).pairs_in(city="islamabad") == [
        ("rst_1", "itm_a", "itm_b", 2)
    ]


async def test_no_index_means_no_pairs(sessions):
    """Nothing maps a restaurant to a city without one, so a pair could not
    be scoped even if the history had it."""
    from ai_assistant.recommend import Recommender

    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a", "itm_b")])
    assert await Recommender(sessions).pairs_in(city="islamabad") == []


async def test_hydrating_no_ids_costs_nothing(sessions):
    from ai_assistant.recommend import Recommender

    assert await Recommender(sessions).hydrate([]) == []


async def test_hydrating_pair_ids_returns_menu_passages(sessions):
    """A combo's dishes are usually not in the recommendation list, so they
    arrive here as bare ids and have to be turned back into passages before
    anything can price them."""
    from ai_assistant.adapters.repo import IndexStateRepo
    from ai_assistant.db import item_chunks
    from ai_assistant.recommend import Recommender

    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version=VERSION, now=NOW)
        await session.execute(
            item_chunks.insert().values(
                id="rst_1:itm_a",
                model_version=VERSION,
                restaurant_id="rst_1",
                item_id="itm_a",
                name="itm_a",
                city="islamabad",
                category="Mains",
                content="Chicken Biryani",
                content_hash="h",
                embedding=[0.0] * 512,
                tags=[],
                cuisines=[],
                price_cents=450,
                available=True,
                status="open",
                updated_at=NOW,
            )
        )
        await session.commit()

    # Duplicates collapse: a dish in two pairs must be looked up once.
    passages = await Recommender(sessions).hydrate(
        [("rst_1", "itm_a"), ("rst_1", "itm_a"), ("rst_1", "itm_gone")]
    )
    assert [(p.item_id, p.restaurant_id) for p in passages] == [("itm_a", "rst_1")]
    assert "Chicken Biryani" in passages[0].text
