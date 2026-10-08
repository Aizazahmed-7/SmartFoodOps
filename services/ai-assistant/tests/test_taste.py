"""Taste profiles and personalised recommendations (FR-75).

Content-based over DECLARED tags and cuisines, so every ranking is
explainable from a profile and a dish alone — which is why the scoring is
pure and tested here without a database.
"""

from datetime import UTC, datetime, timedelta

import pytest
from ai_assistant.adapters.features import FeatureRepo
from ai_assistant.consumers import FeatureHandler, ViewHandler
from ai_assistant.db import item_chunks, metadata
from ai_assistant.domain.taste import (
    MIN_ORDERS,
    Attributes,
    Taste,
    build,
    recommend,
    score,
)
from ai_assistant.profiles import build_profiles
from ai_assistant.recommend import Recommender
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
VERSION = "fake:512"


def _row(
    item="itm_1",
    restaurant="rst_1",
    cuisines=("pakistani",),
    tags=("spicy",),
    qty=1,
    order=None,
):
    """One ordered dish. `order` defaults to a fresh id per row so a list of
    rows reads as separate orders unless a test says otherwise — the
    one-order-many-dishes case is the one that has to be written out."""
    return (order or f"ord_{item}_{restaurant}", item, restaurant, list(cuisines), list(tags), qty)


# ── building a profile ──────────────────────────────────────────────


def test_a_profile_counts_what_was_ordered():
    profile = build([_row("itm_1"), _row("itm_2", tags=("spicy", "halal"))])
    assert dict(profile.cuisines) == {"pakistani": 2}
    assert dict(profile.tags) == {"spicy": 2, "halal": 1}
    assert profile.orders == 2 and set(profile.ordered) == {"itm_1", "itm_2"}


def test_quantity_does_not_rewrite_taste():
    """Buying four naans on one evening is one decision about naan. Letting
    quantity in would let a catering order define somebody's taste — the
    same reason popularity ranks on distinct orders (FR-80)."""
    heavy = build([_row("itm_1", qty=40)])
    light = build([_row("itm_1", qty=1)])
    assert dict(heavy.cuisines) == dict(light.cuisines)


def test_one_order_is_not_a_preference():
    """A "personalised" list built off a single order would differ from the
    baseline with nothing behind the difference — worse than the baseline,
    because it looks like it knows something."""
    assert build([_row()]).thin
    assert not build([_row("itm_1"), _row("itm_2")]).thin
    assert MIN_ORDERS == 2


def test_one_order_of_two_dishes_is_still_one_order():
    """The B4 review's finding. `orders` counted ROWS, so a single order
    containing a curry and a naan reported two and cleared `MIN_ORDERS` —
    shipping exactly what that constant exists to prevent. Both tests that
    should have caught it used one-dish orders."""
    one_order = build([_row("itm_curry", order="ord_1"), _row("itm_naan", order="ord_1")])
    assert one_order.orders == 1 and one_order.thin


def test_two_orders_of_the_same_dish_are_two_orders():
    """The other direction: ordering the same thing on two nights is a
    preference, and counting distinct ids must not collapse it."""
    twice = build([_row("itm_curry", order="ord_1"), _row("itm_curry", order="ord_2")])
    assert twice.orders == 2 and not twice.thin


def test_an_empty_history_is_thin():
    assert build([]).thin


# ── scoring ─────────────────────────────────────────────────────────


LOVES_SPICY_PAKISTANI = build(
    [_row("itm_a", "rst_1"), _row("itm_b", "rst_1", tags=("spicy", "halal"))]
)


def test_a_matching_dish_outscores_a_mismatched_one():
    fit = score(LOVES_SPICY_PAKISTANI, Attributes("itm_new", "rst_1", ["pakistani"], ["spicy"]))
    miss = score(LOVES_SPICY_PAKISTANI, Attributes("itm_odd", "rst_9", ["italian"], ["mild"]))
    assert fit > miss


def test_cuisine_outranks_tag():
    """Cuisine is the coarser and more reliable signal — a `spicy` tag is one
    restaurant's opinion about one dish, and kitchens disagree about it."""
    right_cuisine = score(LOVES_SPICY_PAKISTANI, Attributes("a", "rst_9", ["pakistani"], ["mild"]))
    right_tag = score(LOVES_SPICY_PAKISTANI, Attributes("b", "rst_9", ["italian"], ["spicy"]))
    assert right_cuisine > right_tag


def test_a_dish_with_many_tags_does_not_win_on_label_count():
    """BEST match, not sum: "how much does this customer like the thing this
    dish most is", not "how many labels did the restaurant type"."""
    plain = score(LOVES_SPICY_PAKISTANI, Attributes("a", "rst_9", ["pakistani"], ["spicy"]))
    padded = score(
        LOVES_SPICY_PAKISTANI,
        Attributes("b", "rst_9", ["pakistani"], ["spicy", "halal", "popular", "new"]),
    )
    assert plain == padded


def test_an_unfamiliar_dish_edges_out_one_already_eaten():
    """Both nudges exist because a food recommender has two jobs that pull
    apart: people reorder favourites constantly, and a list of things you
    have already eaten teaches you nothing."""
    ranked = recommend(
        LOVES_SPICY_PAKISTANI,
        [
            Attributes("itm_a", "rst_1", ["pakistani"], ["spicy"]),  # already ordered
            Attributes("itm_new", "rst_1", ["pakistani"], ["spicy"]),  # same fit, new
        ],
        2,
    )
    assert [a.item_id for a in ranked] == ["itm_new", "itm_a"]


def test_a_favourite_still_beats_an_unrelated_novelty():
    """The novelty nudge breaks ties; it does not override taste. A
    recommender that buries the dish somebody actually wants is worse than
    one that repeats itself."""
    ranked = recommend(
        LOVES_SPICY_PAKISTANI,
        [
            Attributes("itm_a", "rst_1", ["pakistani"], ["spicy"]),  # ordered, perfect fit
            Attributes("itm_odd", "rst_9", ["italian"], ["mild"]),  # new, no fit
        ],
        2,
    )
    assert [a.item_id for a in ranked] == ["itm_a", "itm_odd"]


def test_familiarity_breaks_a_tie_without_trapping_anyone():
    """A recommender that only ever returns the place you order from is a
    bookmark."""
    ranked = recommend(
        LOVES_SPICY_PAKISTANI,
        [
            Attributes("itm_x", "rst_9", ["pakistani"], ["spicy"]),
            Attributes("itm_y", "rst_1", ["pakistani"], ["spicy"]),  # familiar kitchen
        ],
        2,
    )
    assert [a.item_id for a in ranked] == ["itm_y", "itm_x"]


def test_two_customers_of_different_sizes_score_on_one_scale():
    """Normalised against the profile's own maximum, or the heaviest user's
    scores would dwarf everyone else's and any threshold tuned on one would
    be wrong for the other."""
    light = build([_row("a"), _row("b")])
    heavy = build([_row(f"itm_{n}") for n in range(200)])
    dish = Attributes("new", "rst_1", ["pakistani"], ["spicy"])
    assert score(light, dish) == pytest.approx(score(heavy, dish))


def test_ranking_is_stable_for_identical_data():
    """A recommendation that reshuffles between two identical requests looks
    broken and cannot be debugged."""
    candidates = [Attributes("b", "rst_9"), Attributes("a", "rst_9")]
    assert [a.item_id for a in recommend(Taste(), candidates, 2)] == ["a", "b"]


# ── the offline build, end to end ───────────────────────────────────


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _menu(sessions, *items):
    async with sessions() as session:
        for item_id, restaurant, cuisines, tags in items:
            await session.execute(
                item_chunks.insert().values(
                    id=f"{restaurant}:{item_id}",
                    restaurant_id=restaurant,
                    item_id=item_id,
                    # FR-88 gave the index a name column; irrelevant here,
                    # but every dish has one.
                    name=f"Dish {item_id}",
                    city="springfield",
                    category="Mains",
                    content=item_id,
                    embedding=[0.0] * 512,
                    tags=list(tags),
                    cuisines=list(cuisines),
                    price_cents=100,
                    available=True,
                    status="open",
                    updated_at=NOW,
                )
            )
        await session.commit()


def _view(restaurant="rst_1", user="usr_1", view_id="v1", at=NOW):
    import json

    payload = {"view_id": view_id, "restaurant_id": restaurant, "viewed_at": at.isoformat()}
    if user:
        payload["user_id"] = user
    return {"event_type": "MenuViewed", "payload": json.dumps(payload)}


def _order(order_id, item, restaurant="rst_1", user="usr_1", at=NOW):
    import json

    return {
        "event_type": "OrderPlaced",
        "payload": json.dumps(
            {
                "order_id": order_id,
                "user_id": user,
                "restaurant_id": restaurant,
                "items": [{"menu_item_id": item, "name": item, "qty": 1}],
                "placed_at": at.isoformat(),
            }
        ),
    }


async def test_a_profile_is_built_from_the_index_not_the_payload(sessions):
    """The payload knows a name as it was sold; the index knows the tags and
    cuisines a restaurant declared — and those are what a content-based
    profile is made of."""
    await _menu(
        sessions,
        ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]),
        ("itm_biryani", "rst_1", ["pakistani"], ["spicy", "rice"]),
    )
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_biryani")]
    )

    summary = await build_profiles(sessions, now=NOW)
    assert summary == {"users": 1, "built": 1}

    async with sessions() as session:
        profile = await FeatureRepo(session).profile("usr_1")
    assert profile is not None
    assert dict(profile.cuisines) == {"pakistani": 2}
    assert profile.tags["spicy"] == 2 and profile.orders == 2


async def test_browsing_adds_familiarity_and_nothing_else(sessions):
    """FR-75's second input. A view says somebody looked at a RESTAURANT,
    not that they wanted a dish."""
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_karahi")]
    )
    await ViewHandler(sessions).handle_batch([_view(restaurant="rst_browsed")])
    await build_profiles(sessions, now=NOW)
    async with sessions() as session:
        profile = await FeatureRepo(session).profile("usr_1")
    assert profile is not None
    assert profile.restaurants["rst_browsed"] == 1  # familiarity
    assert "rst_browsed" not in profile.cuisines and "rst_browsed" not in profile.tags


async def test_an_anonymous_view_is_dropped(sessions):
    """A profile needs somebody to belong to, and a table of views that
    cannot be attributed is a retention obligation with no reader."""
    await ViewHandler(sessions).handle_batch([_view(user="")])
    async with sessions() as session:
        assert await FeatureRepo(session).viewed_restaurants(user_id="", since=NOW) == []


async def test_only_MenuViewed_is_recorded(sessions):
    await ViewHandler(sessions).handle({"event_type": "SomethingElse", "payload": "{}"})
    async with sessions() as session:
        assert await FeatureRepo(session).viewed_restaurants(user_id="usr_1", since=NOW) == []


async def test_rebuilding_is_its_own_retry_policy(sessions):
    """Rebuilt whole rather than updated incrementally: an incremental
    profile is a counter, a counter cannot absorb a replayed order, and a
    profile that drifts cannot be debugged because there is no state to
    compare against."""
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_karahi")]
    )
    await build_profiles(sessions, now=NOW)
    await build_profiles(sessions, now=NOW)
    async with sessions() as session:
        profile = await FeatureRepo(session).profile("usr_1")
    assert profile is not None and profile.orders == 2  # not four


async def test_a_customer_who_stopped_ordering_is_not_rebuilt(sessions):
    """Bounded by the same window the profile is, or the job grows forever."""
    from ai_assistant.domain.taste import WINDOW

    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_old", "itm_karahi", at=NOW - WINDOW - timedelta(days=1))]
    )
    assert (await build_profiles(sessions, now=NOW))["users"] == 0


# ── the read ────────────────────────────────────────────────────────


async def test_a_customer_with_history_gets_a_taste_list(sessions):
    """FR-75: it must DIFFER measurably from the popularity baseline, and
    `basis` is what makes the difference observable."""
    await _menu(
        sessions,
        ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]),
        ("itm_biryani", "rst_1", ["pakistani"], ["spicy"]),
        ("itm_pasta", "rst_2", ["italian"], ["mild"]),
    )
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_karahi")]
    )
    await build_profiles(sessions, now=NOW)

    basis, dishes = await Recommender(sessions).for_user(
        user_id="usr_1", city="springfield", limit=3, at=NOW
    )
    assert basis == "taste"
    # The unfamiliar dish that matches their taste leads; the Italian one
    # they have never shown interest in comes last.
    assert [d.item_id for d in dishes] == ["itm_biryani", "itm_karahi", "itm_pasta"]


async def test_a_customer_with_one_order_gets_the_baseline(sessions):
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch([_order("ord_1", "itm_karahi")])
    await build_profiles(sessions, now=NOW)
    basis, _ = await Recommender(sessions).for_user(
        user_id="usr_1", city="springfield", limit=3, at=NOW
    )
    assert basis == "popular"


async def test_a_customer_with_no_profile_at_all_gets_the_baseline(sessions):
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    basis, _ = await Recommender(sessions).for_user(
        user_id="usr_stranger", city="springfield", limit=3, at=NOW
    )
    assert basis == "popular"


async def test_a_city_with_no_menu_falls_back_to_the_baseline(sessions):
    """A profile cannot rank a menu that is not there, and returning
    somebody else's city would be worse than returning nothing."""
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_karahi")]
    )
    await build_profiles(sessions, now=NOW)
    basis, dishes = await Recommender(sessions).for_user(
        user_id="usr_1", city="nowhereville", limit=3, at=NOW
    )
    assert basis == "popular" and dishes == []


async def test_a_profile_over_an_empty_menu_recommends_nothing(sessions):
    """The city has an index and the customer has a profile, but nothing in
    it is currently sellable — an honest empty list, not somebody else's
    city."""
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_karahi")]
    )
    await build_profiles(sessions, now=NOW)
    async with sessions() as session:
        await session.execute(item_chunks.delete())
        await session.commit()
    basis, dishes = await Recommender(sessions).for_user(
        user_id="usr_1", city="springfield", limit=3, at=NOW
    )
    assert basis == "popular" and dishes == []


async def test_a_reindex_mid_pass_does_not_blank_a_good_profile(sessions):
    """`active` is read once at the top of the pass. If a reindex lands
    mid-pass the old version's chunks vanish, every remaining user reads
    zero rows, and an unguarded write would upsert an EMPTY profile over a
    good one (B4 review). A stale profile beats no profile."""
    await _menu(sessions, ("itm_karahi", "rst_1", ["pakistani"], ["spicy"]))
    await FeatureHandler(sessions).handle_batch(
        [_order("ord_1", "itm_karahi"), _order("ord_2", "itm_karahi")]
    )
    await build_profiles(sessions, now=NOW)
    async with sessions() as session:
        before = await FeatureRepo(session).profile("usr_1")
    assert before is not None and before.orders == 2

    # The chunks move to a new vector space; the pass still holds the old.
    async with sessions() as session:
        await session.execute(item_chunks.delete())
        await session.commit()
    await build_profiles(sessions, now=NOW)

    async with sessions() as session:
        after = await FeatureRepo(session).profile("usr_1")
    assert after is not None and after.orders == 2  # untouched, not blanked
