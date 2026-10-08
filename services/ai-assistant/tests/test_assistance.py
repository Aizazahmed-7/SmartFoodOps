"""Order-assistance questions (FR-78).

Three shapes a customer asks once a dish is in front of them. Two are
already answerable from the chunk — it carries the restaurant's own
description and its declared tags — so what is tested here is the third,
and the rule that keeps the other two honest.
"""

from datetime import UTC, datetime

import pytest
from ai_assistant.consumers import FeatureHandler
from ai_assistant.db import item_chunks, metadata, restaurant_chunks
from ai_assistant.domain.assistance import asks_for_pairing
from ai_assistant.recommend import Recommender
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 28, 12, 30, tzinfo=UTC)
VERSION = "fake:512"


# ── which questions are pairing questions ───────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "what goes with the biryani?",
        "what goes well with karahi",
        "what should I get with it?",
        "anything to go alongside?",
        "what is a good side dish?",
        "what pairs with the nihari",
        "what complements this",
    ],
)
def test_a_pairing_question_is_recognised(question: str):
    assert asks_for_pairing(question)


@pytest.mark.parametrize(
    "question",
    [
        "what is in the biryani?",
        "is it spicy?",
        "how spicy is the karahi",
        "what do you recommend?",
        "something light and cooling",
    ],
)
def test_an_ordinary_question_is_not_a_pairing_question(question: str):
    """These are already answered by the chunk itself — pulling co-ordered
    dishes in would dilute the candidate list for no gain."""
    assert not asks_for_pairing(question)


# ── what actually goes with a dish ──────────────────────────────────


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            restaurant_chunks.insert().values(
                id="rst_1:self",
                restaurant_id="rst_1",
                city="islamabad",
                brand_id=None,
                cuisines=[],
                status="open",
                content="Biryani House",
                embedding=[0.0] * 512,
                updated_at=NOW,
            )
        )
        for item_id, name in (
            ("itm_biryani", "Chicken Biryani"),
            ("itm_naan", "Garlic Naan"),
            ("itm_raita", "Raita"),
            ("itm_lonely", "Kachumber Salad"),
        ):
            await session.execute(
                item_chunks.insert().values(
                    id=f"rst_1:{item_id}",
                    restaurant_id="rst_1",
                    item_id=item_id,
                    # FR-88 gave the index a name column; irrelevant here,
                    # but every dish has one.
                    name=f"Dish {item_id}",
                    city="islamabad",
                    category="Mains",
                    content=name,
                    embedding=[0.0] * 512,
                    tags=[],
                    cuisines=[],
                    price_cents=100,
                    available=True,
                    status="open",
                    updated_at=NOW,
                )
            )
        await session.commit()
    yield maker
    await engine.dispose()


def _placed(order_id, *items, at=NOW):
    import json

    return {
        "event_type": "OrderPlaced",
        "payload": json.dumps(
            {
                "order_id": order_id,
                "user_id": "usr_1",
                "restaurant_id": "rst_1",
                "items": [{"menu_item_id": i, "name": i, "qty": 1} for i in items],
                "placed_at": at.isoformat(),
            }
        ),
    }


async def test_the_partners_are_what_people_actually_order_with_it(sessions):
    """Not "what is similar" — naan and biryani are not alike, they are
    bought together, and no amount of semantic similarity finds that."""
    await FeatureHandler(sessions).handle_batch(
        [
            _placed("o1", "itm_biryani", "itm_naan"),
            _placed("o2", "itm_biryani", "itm_naan"),
            _placed("o3", "itm_biryani", "itm_raita"),
        ]
    )
    partners = await Recommender(sessions).goes_with(
        item_ids=["itm_biryani"], city="islamabad", limit=5
    )
    # Naan twice, raita once — ranked by how often the pairing happened.
    assert [p.item_id for p in partners] == ["itm_naan", "itm_raita"]


async def test_the_dish_asked_about_is_not_its_own_accompaniment(sessions):
    await FeatureHandler(sessions).handle_batch([_placed("o1", "itm_biryani", "itm_naan")])
    partners = await Recommender(sessions).goes_with(
        item_ids=["itm_biryani", "itm_naan"], city="islamabad", limit=5
    )
    assert partners == []


async def test_a_dish_on_either_side_of_the_pair_is_found(sessions):
    """Pairs are stored canonically (`a < b`), so the dish asked about can
    be on either end — checking one would find half the partners."""
    await FeatureHandler(sessions).handle_batch([_placed("o1", "itm_biryani", "itm_naan")])
    from_left = await Recommender(sessions).goes_with(
        item_ids=["itm_biryani"], city="islamabad", limit=5
    )
    from_right = await Recommender(sessions).goes_with(
        item_ids=["itm_naan"], city="islamabad", limit=5
    )
    assert [p.item_id for p in from_left] == ["itm_naan"]
    assert [p.item_id for p in from_right] == ["itm_biryani"]


async def test_a_dish_nobody_pairs_has_no_accompaniment(sessions):
    """Honest rather than invented: the turn answers from the dish alone,
    which is a worse answer but not a wrong one."""
    await FeatureHandler(sessions).handle_batch([_placed("o1", "itm_biryani", "itm_naan")])
    assert (
        await Recommender(sessions).goes_with(item_ids=["itm_lonely"], city="islamabad", limit=5)
        == []
    )


async def test_asking_about_nothing_costs_no_query(sessions):
    assert await Recommender(sessions).goes_with(item_ids=[], city="islamabad", limit=5) == []
