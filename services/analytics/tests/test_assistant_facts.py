"""One fact row per assistant interaction (FR-94).

`c1.assistant.events` has been published through the outbox since B3 and
read by nobody. Everything B7 measures starts here, so the properties worth
testing are the ones that decide whether a KPI can be trusted: a redelivery
converges, a failure counts, and one unreadable row cannot stop the rest.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from analytics.consumers import AssistantFactsProjector, assistant_values
from analytics.db import assistant_facts, metadata
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


async def _projector() -> tuple[AssistantFactsProjector, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return AssistantFactsProjector(sessions), sessions


def _event(message_id="msg_1", **overrides):
    payload = {
        "message_id": message_id,
        "conversation_id": "cnv_1",
        "user_id": "usr_1",
        "city": "islamabad",
        "outcome": "answered",
        "refusal_reason": "none",
        "cache_tier": "",
        "item_ids": ["itm_a", "itm_b"],
        "restaurant_ids": ["rst_1"],
        "candidates": 8,
        "ungrounded": 1,
        "duration_ms": 1840.5,
    }
    payload.update(overrides)
    return {
        "event_type": "AssistantInteraction",
        "aggregate_type": "interaction",
        "aggregate_id": message_id,
        "occurred_at": T0.isoformat(),
        "payload": payload,
    }


async def _rows(sessions):
    async with sessions() as session:
        return (await session.execute(sa.select(assistant_facts))).all()


# ── the fold ───────────────────────────────────────────────────────


async def test_one_interaction_becomes_one_fact():
    projector, sessions = await _projector()
    await projector.handle_batch([_event()])
    [row] = await _rows(sessions)
    assert row.message_id == "msg_1"
    assert row.outcome == "answered"
    assert row.duration_ms == 1840.5
    assert list(row.item_ids) == ["itm_a", "itm_b"]
    assert list(row.restaurant_ids) == ["rst_1"]
    assert row.candidates == 8 and row.ungrounded == 1


async def test_a_redelivery_converges_rather_than_counting_twice():
    """The whole reason this is a fact and not a counter. The outbox is
    at-least-once and its poller re-sends anything it published but did not
    mark — `+1 question` applied twice is a lie no natural key saves."""
    projector, sessions = await _projector()
    await projector.handle_batch([_event()])
    await projector.handle_batch([_event()])
    assert len(await _rows(sessions)) == 1


async def test_duplicate_keys_inside_one_batch_do_not_abort_it():
    """A batch spanning a replayed partition routinely carries the same
    message twice. The last write wins, which is the order the partition
    delivered them in."""
    projector, sessions = await _projector()
    await projector.handle_batch([_event(outcome="answered"), _event(outcome="failed")])
    [row] = await _rows(sessions)
    assert row.outcome == "failed"


async def test_a_restated_interaction_updates_rather_than_being_ignored():
    """Unlike an item fact, an interaction CAN legitimately be restated: a
    turn that streamed and then failed at the last token settles twice, and
    the second fact is the true one."""
    projector, sessions = await _projector()
    await projector.handle_batch([_event(outcome="answered", duration_ms=1200.0)])
    await projector.handle_batch([_event(outcome="failed", duration_ms=30000.0)])
    [row] = await _rows(sessions)
    assert row.outcome == "failed" and row.duration_ms == 30000.0


# ── what must be counted ───────────────────────────────────────────


@pytest.mark.parametrize("outcome", ["answered", "refused", "no_match", "failed"])
async def test_every_outcome_is_recorded_including_the_bad_ones(outcome):
    """A KPI that counted only successes would improve every time the
    provider got worse — the one direction a metric must never move for
    free."""
    projector, sessions = await _projector()
    await projector.handle_batch([_event(outcome=outcome)])
    assert (await _rows(sessions))[0].outcome == outcome


# ── forward compatibility and bad data ─────────────────────────────


async def test_another_services_event_on_this_topic_is_skipped():
    projector, sessions = await _projector()
    await projector.handle_batch([{"event_type": "SomethingElse", "payload": {}}])
    assert await _rows(sessions) == []


async def test_one_unreadable_payload_does_not_stop_the_batch():
    """A projection that parks on one bad row stops counting EVERYTHING,
    and the number a dashboard then shows silently describes a shorter
    window than its label claims."""
    projector, sessions = await _projector()
    bad = _event()
    bad["payload"] = {"conversation_id": "cnv_x"}  # no message_id
    await projector.handle_batch([bad, _event("msg_good")])
    assert [row.message_id for row in await _rows(sessions)] == ["msg_good"]


async def test_an_empty_batch_writes_nothing():
    projector, sessions = await _projector()
    await projector.handle_batch([])
    assert await _rows(sessions) == []


def test_a_payload_with_no_message_id_has_no_row():
    assert assistant_values({"user_id": "usr_1"}, T0.isoformat()) is None
    assert assistant_values({"message_id": ""}, T0.isoformat()) is None


def test_missing_optional_fields_default_rather_than_raising():
    """First-party drift must cost a field, never the batch."""
    values = assistant_values({"message_id": "msg_1"}, T0.isoformat())
    assert values is not None
    assert values["item_ids"] == [] and values["candidates"] == 0
    assert values["duration_ms"] == 0.0 and values["refusal_reason"] == "none"


async def test_a_single_event_folds_through_the_same_path_as_a_batch():
    """The consumer calls `handle` for non-batched delivery; it must not
    be a second implementation that can drift from `handle_batch`."""
    projector, sessions = await _projector()
    await projector.handle(_event("msg_single"))
    assert [row.message_id for row in await _rows(sessions)] == ["msg_single"]


# ── the skip contract, for every field and not just one ────────────


async def test_a_batch_survives_every_shape_of_unreadable_payload():
    """The docstring promises "None rather than an exception on a shapeless
    payload … a KPI projection that stops on one bad row stops counting
    everything". That held for `message_id` alone: every other coercion was
    unguarded, so a missing envelope timestamp, a non-numeric duration or a
    non-iterable id list raised out of `handle_batch` and took the GOOD
    facts in the same batch with it — and the consumer group stopped
    advancing, which is the silent-short-window failure this table exists to
    avoid."""
    projector, sessions = await _projector()
    broken = [
        {**_event("bad_ts"), "occurred_at": ""},
        _event("bad_candidates", candidates="eight"),
        _event("bad_duration", duration_ms="fast"),
        _event("bad_items", item_ids=7),
    ]
    await projector.handle_batch([_event("good_1"), *broken, _event("good_2")])
    rows = await _rows(sessions)
    assert {row.message_id for row in rows} == {"good_1", "good_2"}


def test_an_explicit_null_user_is_not_the_string_none():
    """`.get(key, "")` only defaults when the KEY IS ABSENT. An explicit
    null produced the literal "None", which passes every `user_id != ""`
    guard downstream — so two unrelated anonymous turns became one customer
    and would join to any order row carrying the same stringified null."""
    values = assistant_values(
        {"message_id": "m1", "user_id": None, "conversation_id": None, "city": None},
        T0.isoformat(),
    )
    assert values is not None
    assert values["user_id"] == "" and values["conversation_id"] == "" and values["city"] == ""
