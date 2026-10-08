"""The dishes an answer named, priced live (FR-60).

The answer's prose carries no price — that is what makes it cacheable and
what makes a reconnect safe. These are the rules for turning the ids it does
carry into something a customer can tap, and the ones worth testing are the
failures: a restaurant that has gone, a kitchen that is shut, a dish that is
86'd, and a message that is not yours.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from ai_assistant.adapters.conversations import COMPLETE, STREAMING, ConversationRepo
from ai_assistant.cards import CardService
from ai_assistant.db import item_chunks, metadata
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
VERSION = "fake:512"


def _snapshot(*items: dict[str, Any], **restaurant: Any) -> dict[str, Any]:
    base = {
        "id": "rst_1",
        "name": "Biryani House",
        "display_name": "Biryani House — Downtown",
        "status": "open",
        "open_now": True,
    }
    base.update(restaurant)
    return {"restaurant": base, "items": list(items)}


def _item(item_id="itm_raita", name="Raita", price=350, available=True) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "price_cents": price,
        "currency": "USD",
        "available": available,
        "modifier_groups": [],
    }


class FakeCatalog:
    def __init__(self, by_restaurant: dict[str, Any] | None = None):
        self.by_restaurant = by_restaurant or {}
        self.calls: list[tuple[str, list[str]]] = []

    async def snapshot(self, restaurant_id: str, item_ids: list[str]):
        self.calls.append((restaurant_id, list(item_ids)))
        return self.by_restaurant.get(restaurant_id)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _cards(service, *, message_id="msg_1", user="usr_1") -> list[dict[str, Any]]:
    """The read, with the not-yours case excluded — every test below has
    already established that the message is this customer's."""
    found = await service.for_message(message_id=message_id, user_id=user)
    assert found is not None
    return found


async def _answer(sessions, item_ids, *, user="usr_1", message_id="msg_1"):
    async with sessions() as session:
        repo = ConversationRepo(session)
        await repo.ensure_conversation(
            conversation_id="cnv_1", user_id=user, city="springfield", now=T0
        )
        await repo.start_message(
            message_id=message_id,
            conversation_id="cnv_1",
            role="assistant",
            content="",
            status=STREAMING,
            now=T0,
        )
        await repo.finish_message(
            message_id=message_id, content="Try the Raita.", status=COMPLETE, item_ids=item_ids
        )
        await session.commit()


async def _index(sessions, mapping: dict[str, str]):
    """The index knows which restaurant sells which dish — no second copy
    on the message row to disagree with it."""
    async with sessions() as session:
        for item_id, restaurant_id in mapping.items():
            await session.execute(
                item_chunks.insert().values(
                    id=f"{restaurant_id}:{item_id}",
                    model_version=VERSION,
                    restaurant_id=restaurant_id,
                    item_id=item_id,
                    # FR-88 gave the index a name column; irrelevant here,
                    # but every dish has one.
                    name=f"Dish {item_id}",
                    city="springfield",
                    category="Mains",
                    content=item_id,
                    content_hash="h",
                    embedding=[0.0] * 512,
                    tags=[],
                    cuisines=[],
                    price_cents=100,
                    available=True,
                    status="open",
                    updated_at=T0,
                )
            )
        await session.commit()


# ── the ordinary case ───────────────────────────────────────────────


async def test_a_cited_dish_becomes_a_card_priced_now(sessions):
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item())})

    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["item_id"] == "itm_raita" and card["name"] == "Raita"
    assert card["price_cents"] == 350 and card["currency"] == "USD"
    assert card["orderable"] is True
    assert card["restaurant_name"] == "Biryani House — Downtown"


async def test_cards_come_back_in_the_order_the_answer_cited_them(sessions):
    """Grouping by restaurant is a transport detail. A customer reading
    "first the Raita, then the Karahi" should see them in that order."""
    await _answer(sessions, ["itm_raita", "itm_karahi"])
    await _index(sessions, {"itm_raita": "rst_2", "itm_karahi": "rst_1"})
    catalog = FakeCatalog(
        {
            "rst_1": _snapshot(_item("itm_karahi", "Chicken Karahi", 1200)),
            "rst_2": _snapshot(_item(), id="rst_2"),
        }
    )
    cards = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert [c["item_id"] for c in cards] == ["itm_raita", "itm_karahi"]


async def test_one_snapshot_call_per_restaurant(sessions):
    """Two dishes from one kitchen is one call, not two — the snapshot
    endpoint reads a consistent view and asking twice could straddle a
    price edit."""
    await _answer(sessions, ["itm_raita", "itm_karahi"])
    await _index(sessions, {"itm_raita": "rst_1", "itm_karahi": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item(), _item("itm_karahi", "Karahi", 1200))})
    await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert catalog.calls == [("rst_1", ["itm_raita", "itm_karahi"])]


# ── the failures that must not fail the read ────────────────────────


async def test_a_restaurant_that_has_gone_costs_a_card_not_the_answer(sessions):
    """The prose was true when it was written. A restaurant deleted since
    is a missing card, never a 500 over an answer already on screen."""
    await _answer(sessions, ["itm_raita", "itm_karahi"])
    await _index(sessions, {"itm_raita": "rst_1", "itm_karahi": "rst_gone"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item())})  # rst_gone answers None

    cards = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert [c["item_id"] for c in cards] == ["itm_raita"]


async def test_an_item_the_index_no_longer_knows_is_skipped(sessions):
    await _answer(sessions, ["itm_raita", "itm_removed"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item())})
    cards = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert [c["item_id"] for c in cards] == ["itm_raita"]


async def test_an_86d_dish_is_shown_but_not_orderable(sessions):
    """Shown, because the answer named it and hiding it would make the
    prose refer to a card that is not there."""
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item(available=False))})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["available"] is False and card["orderable"] is False


async def test_a_closed_kitchen_keeps_the_dish_available(sessions):
    """Two different reasons a card cannot be added, kept apart: a single
    "unavailable" would tell a customer to give up on a dish that is back
    at 6pm."""
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item(), open_now=False)})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["available"] is True  # the dish is fine
    assert card["open_now"] is False and card["orderable"] is False  # the kitchen is not


async def test_a_paused_restaurant_is_not_orderable(sessions):
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item(), status="paused")})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["orderable"] is False


async def test_a_missing_open_now_is_not_read_as_closed(sessions):
    """It is None for a brand and absent on an older catalog. Defaulting to
    closed would hide every card behind a field allowed to be missing."""
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item(), open_now=None)})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["orderable"] is True


# ── whose message is it ─────────────────────────────────────────────


async def test_somebody_elses_answer_is_not_readable(sessions):
    """None for both not-yours and not-found, so this cannot be used to
    discover which message ids exist."""
    await _answer(sessions, ["itm_raita"], user="usr_1")
    service = CardService(sessions, FakeCatalog(), model_version=VERSION)
    assert await service.for_message(message_id="msg_1", user_id="usr_2") is None
    assert await service.for_message(message_id="msg_nope", user_id="usr_1") is None


async def test_an_answer_that_cited_nothing_has_no_cards(sessions):
    await _answer(sessions, [])
    assert await _cards(CardService(sessions, FakeCatalog(), model_version=VERSION)) == []


# ── the catalog hop ─────────────────────────────────────────────────


def _client(handler, **kwargs):
    import httpx
    from ai_assistant.adapters.catalog_client import CatalogClient

    return CatalogClient(
        "http://catalog.svc",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_delay=0.0,
        **kwargs,
    )


async def test_the_snapshot_call_carries_system_identity_and_every_id():
    """ADR-0005: the internal endpoint trusts `X-Auth-*` headers, so the
    caller stamps them. `X-Internal-Caller` is what makes the audit line
    say which service asked."""
    import httpx

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["roles"] = request.headers.get("X-Auth-Roles")
        seen["caller"] = request.headers.get("X-Internal-Caller")
        return httpx.Response(200, json=_snapshot(_item()))

    body = await _client(handler).snapshot("rst_1", ["itm_raita", "itm_karahi"])
    assert body is not None and body["items"][0]["id"] == "itm_raita"
    assert seen["roles"] == "system" and seen["caller"] == "ai-assistant"
    assert "item_ids=itm_raita" in str(seen["url"]) and "item_ids=itm_karahi" in str(seen["url"])


async def test_a_restaurant_catalog_no_longer_has_is_a_missing_card():
    """404 is ordinary here, not exceptional: the index is a projection and
    can name a restaurant catalog has since deleted."""
    import httpx

    assert await _client(lambda _r: httpx.Response(404)).snapshot("rst_gone", ["i"]) is None


async def test_a_server_error_is_retried_then_given_up_on():
    """Two attempts, not order's three: this is a customer's render path,
    not a money path, and the answer is already on their screen."""
    import httpx

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    assert await _client(handler).snapshot("rst_1", ["i"]) is None
    assert calls["n"] == 2


async def test_a_network_failure_is_retried_then_given_up_on():
    import httpx

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("catalog is gone")

    assert await _client(handler).snapshot("rst_1", ["i"]) is None
    assert calls["n"] == 2


async def test_a_contract_bug_is_logged_and_not_retried():
    """A 4xx that is not 404 cannot be fixed by asking again."""
    import httpx

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"error": {"code": "VALIDATION_FAILED"}})

    assert await _client(handler).snapshot("rst_1", ["i"]) is None
    assert calls["n"] == 1


async def test_looking_up_no_items_costs_no_query(sessions):
    from ai_assistant.adapters.vector_store import PostgresVectorStore

    async with sessions() as session:
        store = PostgresVectorStore(session)
        assert await store.restaurants_for(item_ids=[], model_version=VERSION) == {}


async def test_a_dish_with_a_required_choice_is_flagged(sessions):
    """Adding it blind produces a cart line the quote endpoint refuses, with
    no UI to fix it and deletion the only escape (B3 review). The panel
    sends these to the restaurant page instead."""
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    sized = _item()
    sized["modifier_groups"] = [{"id": "g1", "name": "Size", "min_select": 1, "options": []}]
    catalog = FakeCatalog({"rst_1": _snapshot(sized)})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["needs_choice"] is True


async def test_a_dish_with_only_optional_extras_is_addable(sessions):
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    extras = _item()
    extras["modifier_groups"] = [{"id": "g1", "name": "Extras", "min_select": 0, "options": []}]
    catalog = FakeCatalog({"rst_1": _snapshot(extras)})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["needs_choice"] is False and card["orderable"] is True


async def test_restaurants_are_priced_concurrently(sessions):
    """Sequentially, one hung catalog multiplied its ~10s worst case by the
    number of restaurants cited — four kitchens held the browser ~41s."""
    import asyncio

    await _answer(sessions, ["itm_a", "itm_b", "itm_c"])
    await _index(sessions, {"itm_a": "rst_1", "itm_b": "rst_2", "itm_c": "rst_3"})
    inflight, peak = 0, 0

    class Slow:
        async def snapshot(self, restaurant_id: str, item_ids: list[str]):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.01)
            inflight -= 1
            return _snapshot(_item(item_ids[0], "Dish"), id=restaurant_id)

    await _cards(CardService(sessions, Slow(), model_version=VERSION))
    assert peak == 3  # all three in flight at once, not one after another


async def test_a_recommendation_is_priced_by_the_same_path_as_a_citation(sessions):
    """One pricing rule, not one per surface. The restaurant comes off the
    passage because the caller already resolved it."""
    from ai_assistant.domain.retrieval import Passage

    catalog = FakeCatalog({"rst_1": _snapshot(_item())})
    cards = await CardService(sessions, catalog, model_version=VERSION).for_items(
        passages=[Passage("itm_raita", "rst_1", "Raita")]
    )
    assert [c["item_id"] for c in cards] == ["itm_raita"]
    assert cards[0]["price_cents"] == 350 and cards[0]["orderable"] is True


async def test_recommending_nothing_calls_no_catalog(sessions):
    catalog = FakeCatalog()
    assert await CardService(sessions, catalog, model_version=VERSION).for_items(passages=[]) == []
    assert catalog.calls == []


async def test_a_real_card_carries_its_own_price_floor(sessions):
    """Against the REAL CardService, not a fake. The first version of this
    field never reached `cards.py` at all — a formatter reflow made the edit
    miss — and every test still passed, because they all fed the field to a
    `FakeCards` by hand. The route then 500'd on the first live request."""
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    priced = _item()
    priced["modifier_groups"] = [
        {
            "id": "size",
            "name": "Size",
            "min_select": 1,
            "max_select": 1,
            "options": [{"price_delta_cents": 300}, {"price_delta_cents": 500}],
        }
    ]
    catalog = FakeCatalog({"rst_1": _snapshot(priced)})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    # Base 350, cheapest mandatory Size +300.
    assert card["price_cents"] == 350 and card["min_total_cents"] == 650


async def test_a_card_with_no_required_options_floors_at_its_price(sessions):
    await _answer(sessions, ["itm_raita"])
    await _index(sessions, {"itm_raita": "rst_1"})
    catalog = FakeCatalog({"rst_1": _snapshot(_item())})
    (card,) = await _cards(CardService(sessions, catalog, model_version=VERSION))
    assert card["min_total_cents"] == card["price_cents"] == 350
