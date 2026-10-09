"""The interaction fact (FR-94, FR-95).

A KPI, not telemetry — so it leaves through the outbox, in the transaction
that settles the message it describes (ADR-0002). The properties worth
holding are the ones a counting query depends on: exactly one fact per
answer, absolute values so a re-drain converges, and a fact for the turns
that went WRONG as well as the ones that went right.
"""

import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from ai_assistant.adapters.conversations import COMPLETE, STREAMING, ConversationRepo
from ai_assistant.db import messages, metadata, outbox
from ai_assistant.domain.retrieval import Passage
from ai_assistant.turns import Publisher, run_turn
from smartfood_kafka import EventType, Topic, topic
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        repo = ConversationRepo(session)
        await repo.ensure_conversation(
            conversation_id="cnv_1", user_id="usr_1", city="springfield", now=T0
        )
        await repo.start_message(
            message_id="msg_1",
            conversation_id="cnv_1",
            role="assistant",
            content="",
            status=STREAMING,
            now=T0,
        )
        await session.commit()
    yield maker
    await engine.dispose()


class Bus:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, data))


class Graph:
    """A finished turn, as the graph leaves it."""

    def __init__(self, raises: BaseException | None = None, **state):
        self.raises = raises
        self.state = {
            "answer": "Try the Raita.",
            "item_ids": ["itm_raita"],
            "restaurant_ids": ["rst_karachi_grill"],
            "candidates": [Passage("itm_raita", "rst_karachi_grill", "Raita")],
            "dropped": 0,
            **state,
        }

    async def ainvoke(self, state):
        if self.raises:
            raise self.raises
        return self.state


async def _turn(sessions, graph=None, **kwargs):
    await run_turn(
        graph=graph or Graph(),
        publisher=Publisher(Bus().publish, message_id="msg_1"),
        sessions=sessions,
        message_id="msg_1",
        question="something light?",
        city="springfield",
        conversation_id="cnv_1",
        user_id="usr_1",
        **kwargs,
    )


async def _facts(sessions) -> list[dict]:
    async with sessions() as session:
        rows = (await session.execute(sa.select(outbox))).all()
    return [
        {**dict(r._mapping), "payload": _payload(r.payload)}  # noqa: SLF001
        for r in rows
    ]


def _payload(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


# ── the fact ────────────────────────────────────────────────────────


async def test_an_answered_turn_stages_one_fact(sessions):
    await _turn(sessions)
    (row,) = await _facts(sessions)
    assert row["event_type"] == EventType.ASSISTANT_INTERACTION
    assert row["aggregate_type"] == "interaction"
    # Keyed by the message: it is the topic key, so one answer's facts can
    # never interleave with another's.
    assert row["aggregate_id"] == "msg_1"
    assert row["published_at"] is None  # the poller's job, not ours


async def test_the_fact_carries_what_the_six_metrics_need(sessions):
    """FR-95 asks for usage, questions asked/answered, conversion,
    acceptance rate, response time and engagement. Every one of those is a
    field here or a COUNT over these rows."""
    await _turn(sessions)
    fact = (await _facts(sessions))[0]["payload"]
    assert fact["message_id"] == "msg_1"
    assert fact["conversation_id"] == "cnv_1"  # engagement: turns per conversation
    assert fact["user_id"] == "usr_1"  # usage, and the conversion join
    assert fact["city"] == "springfield"
    assert fact["outcome"] == "answered"  # asked vs answered
    assert fact["item_ids"] == ["itm_raita"]  # acceptance rate
    assert fact["restaurant_ids"] == ["rst_karachi_grill"]  # conversion (FR-97)
    assert fact["candidates"] == 1
    assert fact["ungrounded"] == 0
    assert fact["duration_ms"] >= 0  # average response time


async def test_the_question_text_never_leaves_the_service(sessions):
    """A KPI needs counts and ids. Shipping what a customer typed into an
    analytics store puts NFR-32's retention rule on a second copy nobody
    remembers to purge."""
    await _turn(sessions)
    assert "something light?" not in json.dumps((await _facts(sessions))[0]["payload"])


async def test_values_are_absolute_so_a_redelivery_converges(sessions):
    """The outbox is at-least-once and the poller re-sends anything it
    published but did not mark. A fact saying "+1 question" would inflate
    the KPI every time a poller crashed between those two steps."""
    fact = (await _turn(sessions), await _facts(sessions))[1][0]["payload"]
    assert not any(k.startswith("delta") or k.endswith("_increment") for k in fact)
    assert isinstance(fact["item_ids"], list)  # the set, not "one more item"


# ── exactly once ────────────────────────────────────────────────────


async def test_a_replayed_turn_stages_nothing(sessions):
    """Two facts for one answer is a KPI that double-counts. The guarded
    UPDATE is what prevents it: the second run loses `status = streaming`
    and never reaches the staging call (ADR-0035)."""
    await _turn(sessions)
    await _turn(sessions)
    assert len(await _facts(sessions)) == 1


async def test_a_message_settles_once_and_says_which_call_won(sessions):
    async with sessions() as session:
        repo = ConversationRepo(session)
        assert await repo.finish_message(message_id="msg_1", content="a", status=COMPLETE)
        assert not await repo.finish_message(message_id="msg_1", content="b", status=COMPLETE)
        await session.commit()
    async with sessions() as session:
        content = await session.scalar(
            sa.select(messages.c.content).where(messages.c.id == "msg_1")
        )
    assert content == "a"  # the loser did not overwrite the winner


# ── the turns that went wrong ───────────────────────────────────────


async def test_a_failed_turn_is_a_fact_too(sessions):
    """A KPI that only counted successes would improve every time the
    provider got worse."""
    await _turn(sessions, graph=Graph(raises=RuntimeError("provider down")))
    fact = (await _facts(sessions))[0]["payload"]
    assert fact["outcome"] == "failed"
    assert fact["item_ids"] == [] and fact["restaurant_ids"] == []


@pytest.mark.parametrize("stopped", ["refused", "no_match"])
async def test_a_short_circuited_turn_is_distinguishable(sessions, stopped):
    """ "Asked but not answered" splits three ways, and conflating them
    would hide a broken retriever behind a safety policy working correctly."""
    await _turn(sessions, graph=Graph(stopped=stopped, item_ids=[], restaurant_ids=[]))
    assert (await _facts(sessions))[0]["payload"]["outcome"] == stopped


async def test_a_refusal_records_why(sessions):
    await _turn(sessions, graph=Graph(stopped="refused", refusal_reason="allergen", item_ids=[]))
    assert (await _facts(sessions))[0]["payload"]["refusal_reason"] == "allergen"


# ── where it goes ───────────────────────────────────────────────────


def test_the_topic_is_cell_prefixed_like_every_other():
    assert topic("c1", Topic.ASSISTANT_EVENTS) == "c1.assistant.events"


def test_the_drain_runs_with_the_app_and_stops_with_it():
    """It joins the same task list the consumers use, so shutdown cancels
    it too — a poller outliving the engine is a pass that fails on a closed
    pool once a second, forever."""
    import asyncio

    from ai_assistant.main import create_app
    from fastapi.testclient import TestClient

    from .conftest import FakeBudgetStore, FakeLlm, settings

    class FakePoller:
        def __init__(self):
            self.started = self.cancelled = False

        async def run(self):
            self.started = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    fake = FakePoller()
    app = create_app(
        settings(),
        providers={"anthropic": FakeLlm()},
        budget_store=FakeBudgetStore(),
        runners=[],
        poller=fake,
    )
    with TestClient(app):
        pass
    assert fake.started and fake.cancelled
