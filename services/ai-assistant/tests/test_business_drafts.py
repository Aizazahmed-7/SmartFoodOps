"""Copy about the business (FR-89, FR-90).

The claim FR-90 makes is that engagement copy contains no customer PII, and
these tests check it where it is actually kept: in what the aggregate read
LOADS. A prompt asking a model not to mention customers is a request; a
fact set with no customer in it is a guarantee.
"""

from datetime import UTC, datetime, timedelta

import pytest
from ai_assistant.adapters.repo import IndexStateRepo
from ai_assistant.db import item_chunks, metadata, order_items
from ai_assistant.restaurant_facts import LAPSED_AFTER_DAYS, RestaurantFacts, RestaurantFactsReader
from smartfood_auth import AuthContext, headers_for
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

VERSION = "fake:512"
_INDEXED_AT = datetime(2026, 10, 5, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
OWNER = headers_for(
    AuthContext(sub="usr_owner", roles=frozenset({"restaurant_admin"}), restaurant_id="rst_1")
)
CUSTOMER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
NO_TENANT = headers_for(AuthContext(sub="usr_x", roles=frozenset({"restaurant_admin"})))


def _facts(**kwargs) -> RestaurantFacts:
    base = {
        "orders": 40,
        "customers": 18,
        "repeat_customers": 9,
        "lapsed_customers": 4,
        "top_dishes": ["Chicken Karahi", "Garlic Naan"],
    }
    base.update(kwargs)
    return RestaurantFacts(**base)  # type: ignore[arg-type]


class FakeFacts:
    def __init__(self, facts: RestaurantFacts) -> None:
        self.facts = facts
        self.calls: list[str] = []

    async def for_restaurant(self, claim: str):
        self.calls.append(claim)
        return self.facts


class FakeStore:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return "cdr_1"


@pytest.fixture()
def studio(client):
    facts, store, enqueued = FakeFacts(_facts()), FakeStore(), []
    client.app.state.restaurant_facts = facts
    client.app.state.drafts = store
    client.app.state.enqueue_draft = lambda draft_id, kind: enqueued.append(draft_id)
    return client, facts, store, enqueued


# ── the endpoints ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "kind"), [("promotions", "promotion"), ("engagement", "engagement")]
)
def test_a_business_draft_is_written_from_the_restaurants_own_numbers(studio, path, kind):
    client, facts, store, enqueued = studio
    r = client.post(
        f"/v1/assistant/drafts/{path}",
        json={"request": "something for slow Tuesdays"},
        headers=OWNER,
    )
    assert r.status_code == 202
    assert r.json() == {"draft_id": "cdr_1", "kind": kind}

    created = store.created[0]
    assert created["kind"] == kind
    assert created["request"] == "something for slow Tuesdays"
    assert created["target_id"] is None if "target_id" in created else True
    # Counts and dish names, and nothing else.
    assert created["subject"]["orders"] == 40
    assert created["subject"]["top_dishes"] == ["Chicken Karahi", "Garlic Naan"]
    assert facts.calls == ["rst_1"]
    assert enqueued == ["cdr_1"]


def test_the_subject_contains_no_customer_identity(studio):
    """FR-90's promise, checked on the actual payload rather than trusted
    to the prompt."""
    client, _, store, _ = studio
    client.post("/v1/assistant/drafts/engagement", json={"request": "win them back"}, headers=OWNER)
    subject = store.created[0]["subject"]
    assert set(subject) == {
        "orders",
        "customers",
        "repeat_customers",
        "lapsed_customers",
        "top_dishes",
        "window_days",
        "lapsed_after_days",
    }
    for key, value in subject.items():
        assert isinstance(value, int | list), key


def test_a_restaurant_with_almost_no_orders_is_refused(studio):
    """Copy written from four orders would be a claim dressed as a
    statistic. The same floor FR-92 puts under feedback summaries."""
    client, facts, store, enqueued = studio
    facts.facts = _facts(orders=4)
    r = client.post(
        "/v1/assistant/drafts/engagement", json={"request": "win them back"}, headers=OWNER
    )
    assert r.status_code == 422
    assert store.created == [] and enqueued == []


def test_a_customer_cannot_draft_business_copy(studio):
    client, *_ = studio
    for path in ("promotions", "engagement"):
        r = client.post(f"/v1/assistant/drafts/{path}", json={"request": "x"}, headers=CUSTOMER)
        assert r.status_code in (401, 403)


def test_a_token_naming_no_restaurant_is_refused(studio):
    client, _, store, _ = studio
    r = client.post("/v1/assistant/drafts/promotions", json={"request": "x"}, headers=NO_TENANT)
    assert r.status_code == 403
    assert store.created == []


def test_the_request_sentence_is_required_and_bounded(studio):
    client, *_ = studio
    for body in ({}, {"request": ""}, {"request": "x" * 501}):
        assert (
            client.post("/v1/assistant/drafts/promotions", json=body, headers=OWNER).status_code
            == 422
        ), body


# ── the aggregate read, against a real database ────────────────────


async def _reader() -> tuple[RestaurantFactsReader, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # The readers resolve the ACTIVE generation rather than a configured
    # one, so the fixture has to say which that is — the same thing a
    # reindex does when it flips.
    async with sessions() as session:
        await IndexStateRepo(session).ensure(model_version=VERSION, now=_INDEXED_AT)
        await session.commit()
    return RestaurantFactsReader(sessions), sessions


async def _menu(sessions, restaurant, brand, dishes):
    async with sessions() as session:
        for item_id, name in dishes:
            await session.execute(
                item_chunks.insert().values(
                    id=f"{restaurant}:{item_id}",
                    model_version=VERSION,
                    restaurant_id=restaurant,
                    item_id=item_id,
                    name=name,
                    city="islamabad",
                    brand_id=brand,
                    cuisines=["pakistani"],
                    category="Mains",
                    tags=[],
                    price_cents=900,
                    available=True,
                    status="open",
                    content=name,
                    content_hash=item_id,
                    embedding=[0.0] * 512,
                    updated_at=NOW,
                )
            )
        await session.commit()


async def _order(sessions, order_id, user_id, item_id, restaurant, days_ago, qty=1):
    async with sessions() as session:
        await session.execute(
            order_items.insert().values(
                order_id=order_id,
                item_id=item_id,
                restaurant_id=restaurant,
                user_id=user_id,
                qty=qty,
                placed_at=NOW - timedelta(days=days_ago),
            )
        )
        await session.commit()


async def test_the_aggregates_are_counts_and_dish_names_only():
    reader, sessions = await _reader()
    await _menu(sessions, "rst_1", "brn_1", [("itm_a", "Chicken Karahi"), ("itm_b", "Naan")])
    for n in range(6):
        await _order(sessions, f"ord_{n}", f"usr_{n % 3}", "itm_a", "rst_1", days_ago=n, qty=2)
    await _order(sessions, "ord_x", "usr_0", "itm_b", "rst_1", days_ago=1)

    facts = await reader.for_restaurant("rst_1", now=NOW)
    assert facts.orders == 7
    assert facts.customers == 3
    assert facts.repeat_customers == 3  # each of the three ordered twice+
    assert facts.top_dishes == ["Chicken Karahi", "Naan"]
    assert not facts.thin


async def test_a_lapsed_customer_is_counted_not_named():
    reader, sessions = await _reader()
    await _menu(sessions, "rst_1", "brn_1", [("itm_a", "Karahi")])
    await _order(sessions, "ord_old", "usr_gone", "itm_a", "rst_1", days_ago=LAPSED_AFTER_DAYS + 5)
    for n in range(5):
        await _order(sessions, f"ord_{n}", "usr_here", "itm_a", "rst_1", days_ago=n)

    facts = await reader.for_restaurant("rst_1", now=NOW)
    assert facts.lapsed_customers == 1
    # The id never leaves the database.
    assert "usr_gone" not in str(facts.as_subject())


async def test_another_restaurants_orders_are_not_counted():
    reader, sessions = await _reader()
    await _menu(sessions, "rst_1", "brn_1", [("itm_a", "Mine")])
    await _menu(sessions, "rst_9", "brn_9", [("itm_z", "Theirs")])
    for n in range(6):
        await _order(sessions, f"ord_mine_{n}", f"usr_{n}", "itm_a", "rst_1", days_ago=n)
    for n in range(20):
        await _order(sessions, f"ord_theirs_{n}", f"usr_t{n}", "itm_z", "rst_9", days_ago=n)

    facts = await reader.for_restaurant("rst_1", now=NOW)
    assert facts.orders == 6
    assert facts.top_dishes == ["Mine"]


async def test_a_brand_claim_sums_its_branches():
    """A brand may ask about its business, which is every branch's business
    (ADR-0028). `order_items` only knows branches, so they are resolved
    from the index rather than assumed."""
    reader, sessions = await _reader()
    await _menu(sessions, "rst_1", "brn_1", [("itm_a", "Karahi")])
    await _menu(sessions, "rst_2", "brn_1", [("itm_a", "Karahi")])
    for n in range(3):
        await _order(sessions, f"ord_a{n}", f"usr_{n}", "itm_a", "rst_1", days_ago=n)
    for n in range(3):
        await _order(sessions, f"ord_b{n}", f"usr_{n}", "itm_a", "rst_2", days_ago=n)

    assert (await reader.for_restaurant("brn_1", now=NOW)).orders == 6


async def test_orders_outside_the_window_are_not_counted():
    reader, sessions = await _reader()
    await _menu(sessions, "rst_1", "brn_1", [("itm_a", "Karahi")])
    for n in range(6):
        await _order(sessions, f"ord_old_{n}", f"usr_{n}", "itm_a", "rst_1", days_ago=200)
    facts = await reader.for_restaurant("rst_1", now=NOW)
    assert facts.orders == 0 and facts.thin


async def test_a_claim_matching_no_menu_gets_empty_facts():
    """No branches resolved means nothing to aggregate — and crucially not
    an unscoped read over every restaurant's orders."""
    reader, _ = await _reader()
    facts = await reader.for_restaurant("rst_nobody", now=NOW)
    assert facts.orders == 0 and facts.top_dishes == [] and facts.thin


async def test_aggregates_before_the_first_index_are_empty_not_unscoped():
    """No active generation means no branches resolvable, and the honest
    answer is zeros — not a read across every restaurant's orders."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    reader = RestaurantFactsReader(async_sessionmaker(engine, expire_on_commit=False))
    facts = await reader.for_restaurant("rst_1", now=NOW)
    assert facts.orders == 0 and facts.thin
