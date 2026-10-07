"""Recommendation acceptance (FR-79).

The requirement's own words: acceptance is "a join to ordered items within
an attribution window, NOT a client-reported boolean". So every test here is
really about one thing — nothing in this path takes a surface's word for
its own success.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.attribution import AttributionRepo
from ai_assistant.consumers import FeatureHandler
from ai_assistant.db import metadata, outbox, recommendation_acceptances
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 28, 12, 30, tzinfo=UTC)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _show(sessions, *item_ids, user="usr_1", at=NOW, surface="recommendations") -> str:
    async with sessions() as session:
        shown_id = await AttributionRepo(session).record_shown(
            user_id=user,
            city="islamabad",
            surface=surface,
            basis="taste",
            item_ids=list(item_ids),
            now=at,
        )
        await session.commit()
    # The repo returns `str | None` because a showing that cannot be written
    # must not fail a customer's request. In a test that is a bug, not a
    # degradation: every caller below joins an acceptance to this id.
    assert shown_id is not None
    return shown_id


def _placed(order_id, *items, user="usr_1", at=NOW):
    return {
        "event_type": "OrderPlaced",
        "payload": json.dumps(
            {
                "order_id": order_id,
                "user_id": user,
                "restaurant_id": "rst_1",
                "items": [{"menu_item_id": i, "name": i, "qty": 1} for i in items],
                "placed_at": at.isoformat(),
            }
        ),
    }


async def _events(sessions, kind=None):
    async with sessions() as session:
        rows = (await session.execute(sa.select(outbox).order_by(outbox.c.occurred_at))).all()
    out = []
    for row in rows:
        if kind and row.event_type != kind:
            continue
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        out.append((row.event_type, payload))
    return out


# ── showing ─────────────────────────────────────────────────────────


async def test_showing_a_recommendation_stages_an_event(sessions):
    await _show(sessions, "itm_a", "itm_b")
    ((kind, payload),) = await _events(sessions)
    assert kind == "RecommendationShown"
    assert payload["item_ids"] == ["itm_a", "itm_b"]
    assert payload["surface"] == "recommendations" and payload["basis"] == "taste"


async def test_showing_nothing_is_not_a_showing(sessions):
    """An empty list would put a zero in the acceptance rate's denominator
    for a moment when the customer was offered nothing to accept."""
    # Called directly rather than through `_show`, which narrows the id to
    # `str` for every other test in this file — here None is the assertion.
    async with sessions() as session:
        assert (
            await AttributionRepo(session).record_shown(
                user_id="usr_1",
                city="islamabad",
                surface="recommendations",
                basis="taste",
                item_ids=[],
                now=NOW,
            )
            is None
        )
        await session.commit()
    assert await _events(sessions) == []


# ── the join ────────────────────────────────────────────────────────


async def test_ordering_a_suggested_dish_is_an_acceptance(sessions):
    shown_id = await _show(sessions, "itm_a", "itm_b")
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a")])

    accepted = await _events(sessions, "RecommendationAccepted")
    assert len(accepted) == 1
    (_, payload) = accepted[0]
    assert payload["recommendation_id"] == shown_id and payload["order_id"] == "ord_1"
    # The INTERSECTION, not a boolean: one of two taken is not the same
    # signal as both.
    assert payload["item_ids"] == ["itm_a"]


async def test_ordering_something_else_is_not_an_acceptance(sessions):
    await _show(sessions, "itm_a")
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_unrelated")])
    assert await _events(sessions, "RecommendationAccepted") == []


async def test_an_order_accepts_a_showing_once_however_many_dishes_it_took(sessions):
    """Three suggested dishes on one receipt is one acceptance of three
    items, not three acceptances — otherwise the rate exceeds 100%."""
    await _show(sessions, "itm_a", "itm_b", "itm_c")
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a", "itm_b", "itm_c")])
    accepted = await _events(sessions, "RecommendationAccepted")
    assert len(accepted) == 1
    assert accepted[0][1]["item_ids"] == ["itm_a", "itm_b", "itm_c"]


async def test_a_redelivered_order_does_not_accept_twice(sessions):
    """At-least-once is the contract and this feeds a RATE. The
    `(shown_id, order_id)` key is what makes the derivation safe."""
    await _show(sessions, "itm_a")
    handler = FeatureHandler(sessions)
    await handler.handle_batch([_placed("ord_1", "itm_a")])
    await handler.handle_batch([_placed("ord_1", "itm_a")])
    assert len(await _events(sessions, "RecommendationAccepted")) == 1


async def test_two_showings_of_the_same_dish_are_both_credited(sessions):
    """Distinct showings are distinct offers — a customer shown a dish in
    the panel and again under an answer saw it twice."""
    await _show(sessions, "itm_a", surface="recommendations")
    await _show(sessions, "itm_a", surface="answer")
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a")])
    assert len(await _events(sessions, "RecommendationAccepted")) == 2


# ── the window ──────────────────────────────────────────────────────


async def test_an_order_outside_the_window_accepts_nothing(sessions):
    """Tomorrow's dinner is not credited to today's suggestion."""
    await _show(sessions, "itm_a", at=NOW - timedelta(hours=5))
    await FeatureHandler(sessions, window=timedelta(hours=2)).handle_batch(
        [_placed("ord_1", "itm_a", at=NOW)]
    )
    assert await _events(sessions, "RecommendationAccepted") == []


async def test_a_showing_after_the_order_cannot_have_caused_it(sessions):
    """On a replayed topic the clock is the only thing that says so — a
    window bounded on one side only would credit a suggestion made after
    the customer had already ordered."""
    await _show(sessions, "itm_a", at=NOW + timedelta(minutes=30))
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a", at=NOW)])
    assert await _events(sessions, "RecommendationAccepted") == []


async def test_another_customers_order_accepts_nothing(sessions):
    await _show(sessions, "itm_a", user="usr_1")
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a", user="usr_2")])
    assert await _events(sessions, "RecommendationAccepted") == []


async def test_an_order_with_no_customer_is_not_attributed(sessions):
    await _show(sessions, "itm_a", user="")
    await FeatureHandler(sessions).handle_batch([_placed("ord_1", "itm_a", user="")])
    assert await _events(sessions, "RecommendationAccepted") == []


# ── the guard ───────────────────────────────────────────────────────


async def test_claiming_a_pair_twice_stages_only_one_event(sessions):
    """The staging site's guard, tested directly: the event is written only
    by the call that won the insert (ADR-0035)."""
    shown_id = await _show(sessions, "itm_a")
    async with sessions() as session:
        repo = AttributionRepo(session)
        assert await repo.record_acceptance(
            shown_id=shown_id, order_id="ord_1", item_ids=["itm_a"], accepted_at=NOW, now=NOW
        )
        assert not await repo.record_acceptance(
            shown_id=shown_id, order_id="ord_1", item_ids=["itm_a"], accepted_at=NOW, now=NOW
        )
        await session.commit()
    assert len(await _events(sessions, "RecommendationAccepted")) == 1
    async with sessions() as session:
        rows = (await session.execute(sa.select(recommendation_acceptances))).all()
    assert len(rows) == 1


async def test_failing_to_record_a_showing_does_not_cost_the_customer_the_list(sessions):
    """The recommendation is already computed and about to be rendered.
    What a failure costs is one missing denominator in the acceptance rate,
    which is the right way round."""
    from ai_assistant.recommend import Recommender

    class Broken:
        async def __aenter__(self):
            raise RuntimeError("assistant_db is gone")

        async def __aexit__(self, *exc):  # pragma: no cover — never entered
            return False

    recommender = Recommender(sessions)
    recommender._sessions = cast(Any, lambda: Broken())  # noqa: SLF001
    assert (
        await recommender.record_shown(
            user_id="usr_1", city="islamabad", surface="answer", basis="", item_ids=["itm_a"]
        )
        is None
    )


async def test_a_showing_records_and_returns_its_id(sessions):
    from ai_assistant.recommend import Recommender

    shown_id = await Recommender(sessions).record_shown(
        user_id="usr_1", city="islamabad", surface="answer", basis="", item_ids=["itm_a"]
    )
    assert shown_id is not None and shown_id.startswith("rec_")
    ((kind, payload),) = await _events(sessions)
    assert kind == "RecommendationShown" and payload["surface"] == "answer"


async def test_the_outbox_is_stamped_with_our_clock_not_the_orders(sessions):
    """The poller measures publish lag as `now - occurred_at` and drains in
    that order. Staging with the order's own timestamp made a replayed or
    backlogged order look like hours of publish lag and would fire NFR-6's
    p99<5s alert on a perfectly healthy outbox (B4 review). `accepted_at`
    stays the order's clock, because that is a fact about the order."""
    import sqlalchemy as sa

    shown_id = await _show(sessions, "itm_a")
    long_ago = NOW - timedelta(hours=6)
    async with sessions() as session:
        await AttributionRepo(session).record_acceptance(
            shown_id=shown_id,
            order_id="ord_1",
            item_ids=["itm_a"],
            accepted_at=long_ago,
            now=NOW,
        )
        await session.commit()
    async with sessions() as session:
        staged = (
            await session.execute(
                sa.select(outbox.c.occurred_at).where(
                    outbox.c.event_type == "RecommendationAccepted"
                )
            )
        ).scalar_one()
        stored = (
            await session.execute(sa.select(recommendation_acceptances.c.accepted_at))
        ).scalar_one()
    assert staged.replace(tzinfo=UTC) == NOW  # ours
    assert stored.replace(tzinfo=UTC) == long_ago  # the order's
