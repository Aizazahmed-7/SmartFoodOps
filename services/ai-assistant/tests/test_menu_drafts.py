"""Asking for menu copy (FR-88, UC-25).

Split deliberately: the ENDPOINT is tested over fakes, because what it owns
is the tenancy gate, the bounds and the commit-then-enqueue order; the
FACTS READ is tested against a real database below, because what it owns is
a WHERE clause that doubles as the ownership check.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.repo import IndexStateRepo
from ai_assistant.db import item_chunks, metadata
from ai_assistant.menu_facts import MAX_ITEMS, MenuFacts, MenuFactsReader
from smartfood_auth import AuthContext, headers_for
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

VERSION = "fake:512"
_INDEXED_AT = datetime(2026, 10, 5, tzinfo=UTC)
OWNER = headers_for(
    AuthContext(sub="usr_owner", roles=frozenset({"restaurant_admin"}), restaurant_id="rst_1")
)
CUSTOMER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
NO_TENANT = headers_for(AuthContext(sub="usr_x", roles=frozenset({"restaurant_admin"})))


def _facts(item_id="itm_karahi", name="Chicken Karahi", restaurant="rst_1") -> MenuFacts:
    return MenuFacts(
        item_id=item_id,
        restaurant_id=restaurant,
        brand_id="brn_1",
        name=name,
        category="Mains",
        tags=["spicy"],
        cuisines=["pakistani"],
    )


class FakeReader:
    def __init__(self, *facts: MenuFacts) -> None:
        self.facts = list(facts)
        self.calls: list[dict] = []

    async def for_items(self, *, restaurant_id, item_ids):
        self.calls.append({"restaurant_id": restaurant_id, "item_ids": list(item_ids)})
        return self.facts

    async def for_category(self, *, restaurant_id, category):
        self.calls.append({"restaurant_id": restaurant_id, "category": category})
        return self.facts


class FakeStore:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return f"cdr_{len(self.created)}"


@pytest.fixture()
def studio(client):
    reader, store, enqueued = FakeReader(_facts()), FakeStore(), []
    client.app.state.menu_facts = reader
    client.app.state.drafts = store
    client.app.state.enqueue_draft = lambda draft_id, kind: enqueued.append(draft_id)
    return client, reader, store, enqueued


# ── the endpoint ───────────────────────────────────────────────────


def test_drafting_one_dish_freezes_its_facts_onto_the_row(studio):
    client, reader, store, enqueued = studio
    r = client.post(
        "/v1/assistant/drafts/menu-items",
        json={"item_ids": ["itm_karahi"], "request": "mention the spice"},
        headers=OWNER,
    )
    assert r.status_code == 202
    assert r.json() == {"draft_ids": ["cdr_1"], "queued": 1, "skipped": 0}

    created = store.created[0]
    assert created["kind"] == "menu_item" and created["target_id"] == "itm_karahi"
    assert created["request"] == "mention the spice"
    # FR-88's field list, frozen: a reviewer later sees what the model was
    # told rather than what the menu says today.
    assert created["subject"] == {
        "item_id": "itm_karahi",
        "name": "Chicken Karahi",
        "category": "Mains",
        "tags": ["spicy"],
        "cuisines": ["pakistani"],
    }
    # Committed, THEN enqueued.
    assert enqueued == ["cdr_1"]


def test_the_row_names_the_branch_not_the_claim(studio):
    """A brand token drafting for twelve branches must produce twelve rows
    that each name the menu they are for (ADR-0028)."""
    client, reader, store, _ = studio
    reader.facts = [_facts(restaurant="rst_7")]
    client.post("/v1/assistant/drafts/menu-items", json={"item_ids": ["itm_a"]}, headers=OWNER)
    assert store.created[0]["restaurant_id"] == "rst_7"
    assert store.created[0]["brand_id"] == "brn_1"


def test_a_category_queues_one_row_per_dish(studio):
    client, reader, store, enqueued = studio
    reader.facts = [_facts("itm_a", "A"), _facts("itm_b", "B"), _facts("itm_c", "C")]
    body = client.post(
        "/v1/assistant/drafts/menu-items", json={"category": "Mains"}, headers=OWNER
    ).json()
    assert body["queued"] == 3 and body["skipped"] == 0
    assert len(enqueued) == 3
    assert reader.calls == [{"restaurant_id": "rst_1", "category": "Mains"}]


def test_the_read_is_scoped_to_the_callers_claim(studio):
    """The facts read IS the ownership check — there is no second one."""
    client, reader, _, _ = studio
    client.post("/v1/assistant/drafts/menu-items", json={"item_ids": ["itm_x"]}, headers=OWNER)
    assert reader.calls[0]["restaurant_id"] == "rst_1"


def test_items_that_did_not_come_back_are_a_count_not_a_list(studio):
    """A probe must not be able to distinguish "not yours" from "not
    indexed", or it can map a rival's menu one id at a time."""
    client, reader, _, _ = studio
    reader.facts = [_facts("itm_karahi")]
    body = client.post(
        "/v1/assistant/drafts/menu-items",
        json={"item_ids": ["itm_karahi", "itm_rivals", "itm_imaginary"]},
        headers=OWNER,
    ).json()
    assert body["queued"] == 1 and body["skipped"] == 2
    assert "itm_rivals" not in str(body)


def test_a_request_matching_nothing_queues_nothing(studio):
    client, reader, store, enqueued = studio
    reader.facts = []
    body = client.post(
        "/v1/assistant/drafts/menu-items", json={"item_ids": ["itm_secret"]}, headers=OWNER
    ).json()
    assert body == {"draft_ids": [], "queued": 0, "skipped": 1}
    assert store.created == [] and enqueued == []


# ── gates and bounds ───────────────────────────────────────────────


def test_a_customer_cannot_draft_menu_copy(studio):
    client, *_ = studio
    r = client.post(
        "/v1/assistant/drafts/menu-items", json={"item_ids": ["itm_a"]}, headers=CUSTOMER
    )
    assert r.status_code in (401, 403)


def test_a_token_naming_no_restaurant_is_refused(studio):
    client, _, store, _ = studio
    r = client.post(
        "/v1/assistant/drafts/menu-items", json={"item_ids": ["itm_a"]}, headers=NO_TENANT
    )
    assert r.status_code == 403
    assert store.created == []


@pytest.mark.parametrize("body", [{}, {"item_ids": ["itm_a"], "category": "Mains"}])
def test_exactly_one_of_item_ids_or_category(studio, body):
    client, *_ = studio
    assert (
        client.post("/v1/assistant/drafts/menu-items", json=body, headers=OWNER).status_code == 422
    )


def test_one_click_cannot_become_a_thousand_provider_calls(studio):
    """ "Draft my entire menu" is a legitimate thing to want and an
    illegitimate thing to do in a single request."""
    client, _, store, _ = studio
    r = client.post(
        "/v1/assistant/drafts/menu-items",
        json={"item_ids": [f"itm_{n}" for n in range(MAX_ITEMS + 1)]},
        headers=OWNER,
    )
    assert r.status_code == 422
    assert store.created == []


# ── the facts read, against a real database ────────────────────────


async def _reader() -> tuple[MenuFactsReader, async_sessionmaker]:
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
    return MenuFactsReader(sessions), sessions


async def _chunk(sessions, item_id, name, category, restaurant, brand, version=VERSION):
    async with sessions() as session:
        await session.execute(
            item_chunks.insert().values(
                id=f"{restaurant}:{item_id}:{version}",
                model_version=version,
                restaurant_id=restaurant,
                item_id=item_id,
                name=name,
                city="islamabad",
                brand_id=brand,
                cuisines=["pakistani"],
                category=category,
                tags=["spicy"] if category == "Mains" else [],
                price_cents=900,
                available=True,
                status="open",
                content=name,
                content_hash=f"{item_id}:{version}",
                embedding=[0.0] * 512,
                updated_at=sa.func.now(),
            )
        )
        await session.commit()


async def _seeded():
    reader, sessions = await _reader()
    await _chunk(sessions, "itm_karahi", "Chicken Karahi", "Mains", "rst_1", "brn_1")
    await _chunk(sessions, "itm_naan", "Garlic Naan", "Breads", "rst_1", "brn_1")
    await _chunk(sessions, "itm_secret", "Rival Special", "Mains", "rst_9", "brn_9")
    return reader, sessions


async def test_the_facts_are_fr88s_field_list():
    reader, _ = await _seeded()
    [fact] = await reader.for_items(restaurant_id="rst_1", item_ids=["itm_karahi"])
    assert fact.name == "Chicken Karahi"
    assert fact.category == "Mains"
    assert fact.tags == ["spicy"] and fact.cuisines == ["pakistani"]
    # NOT price, NOT availability: copy that mentions a price is wrong the
    # next time anyone edits it.
    assert "price" not in fact.as_subject()
    assert "available" not in fact.as_subject()


async def test_a_rivals_item_simply_does_not_come_back():
    reader, _ = await _seeded()
    assert await reader.for_items(restaurant_id="rst_1", item_ids=["itm_secret"]) == []
    assert await reader.for_category(restaurant_id="rst_1", category="Mains") != []
    assert [
        f.item_id for f in await reader.for_category(restaurant_id="rst_1", category="Mains")
    ] == ["itm_karahi"]


async def test_a_brand_claim_reaches_its_branches_dishes():
    """The claim may name a BRAND or a branch (ADR-0028), so it is checked
    against both columns."""
    reader, _ = await _seeded()
    [fact] = await reader.for_items(restaurant_id="brn_1", item_ids=["itm_karahi"])
    assert fact.restaurant_id == "rst_1" and fact.brand_id == "brn_1"


async def test_only_the_active_model_version_is_read():
    """A rolling reindex writes the new model's rows BESIDE the old ones,
    so an unversioned read would return every dish twice."""
    reader, sessions = await _seeded()
    await _chunk(sessions, "itm_karahi", "Chicken Karahi", "Mains", "rst_1", "brn_1", "old:256")
    assert len(await reader.for_items(restaurant_id="rst_1", item_ids=["itm_karahi"])) == 1


async def test_an_empty_id_list_asks_the_database_nothing():
    reader, _ = await _reader()
    assert await reader.for_items(restaurant_id="rst_1", item_ids=[]) == []


async def test_a_category_read_is_capped():
    """A category's size is the restaurant's choice, so the bound cannot
    live in the request schema."""
    reader, sessions = await _reader()
    for n in range(5):
        await _chunk(sessions, f"itm_{n}", f"Dish {n}", "Mains", "rst_1", "brn_1")
    assert len(await reader.for_category(restaurant_id="rst_1", category="Mains", limit=3)) == 3


async def test_one_dish_produces_one_fact_however_many_branches_hold_it():
    """A brand's branches each hold their own chunk row for the same
    inherited dish (ADR-0028). Found live: a brand claim asking about one
    dish matched it once per branch and queued four identical drafts —
    four provider calls for one sentence about one dish."""
    reader, sessions = await _reader()
    for branch in ("rst_1", "rst_2", "rst_3", "rst_4"):
        await _chunk(sessions, "itm_karahi", "Chicken Karahi", "Mains", branch, "brn_1")

    facts = await reader.for_items(restaurant_id="brn_1", item_ids=["itm_karahi"])
    assert len(facts) == 1
    assert facts[0].name == "Chicken Karahi"
    assert [
        f.item_id for f in await reader.for_category(restaurant_id="brn_1", category="Mains")
    ] == ["itm_karahi"]


async def test_the_cap_counts_dishes_not_branch_rows():
    """The SQL LIMIT applies before the dedupe, so a cap applied there
    would fill the budget with duplicates and silently drop real dishes."""
    reader, sessions = await _reader()
    for n in range(5):
        for branch in ("rst_1", "rst_2", "rst_3"):
            await _chunk(sessions, f"itm_{n}", f"Dish {n}", "Mains", branch, "brn_1")

    facts = await reader.for_category(restaurant_id="brn_1", category="Mains", limit=4)
    assert len(facts) == 4
    assert len({f.item_id for f in facts}) == 4


async def test_a_service_with_no_index_yet_reads_nothing():
    """`IndexStateRepo.active()` is None before the first drain. Reading
    every generation at once would be the alternative, and a studio that
    drafted from a half-built index is worse than one that says it has
    nothing."""
    reader, _ = await _reader_without_index()
    assert await reader.for_items(restaurant_id="rst_1", item_ids=["itm_a"]) == []
    assert await reader.for_category(restaurant_id="rst_1", category="Mains") == []


async def _reader_without_index() -> tuple[MenuFactsReader, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return MenuFactsReader(sessions), sessions
