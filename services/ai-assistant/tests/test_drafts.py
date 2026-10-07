"""The content studio's job lifecycle (FR-88..FR-93, UC-25).

The queue is at-least-once by configuration (acks_late), so the tests that
matter are the ones about a job arriving twice, arriving late, or arriving
after a human has already acted.
"""

import pytest
import sqlalchemy as sa
from ai_assistant.db import content_drafts, metadata
from ai_assistant.drafts import Claim, DraftNotFound, DraftStore, WrongState
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

OWNER = Claim(restaurant_id="rst_1", brand_id="brn_1")
BRAND_ONLY = Claim(restaurant_id=None, brand_id="brn_1")
RIVAL = Claim(restaurant_id="rst_9", brand_id="brn_9")


async def _store() -> tuple[DraftStore, async_sessionmaker]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return DraftStore(sessions), sessions


async def _row(sessions, draft_id):
    async with sessions() as s:
        return (
            await s.execute(sa.select(content_drafts).where(content_drafts.c.draft_id == draft_id))
        ).one()


async def _new(store, **kwargs) -> str:
    return await store.create(
        restaurant_id=kwargs.pop("restaurant_id", "rst_1"),
        brand_id=kwargs.pop("brand_id", "brn_1"),
        kind=kwargs.pop("kind", "menu_item"),
        **kwargs,
    )


# ── the happy path ─────────────────────────────────────────────────


async def test_a_new_draft_is_queued_and_empty():
    """The row is committed BEFORE anything is enqueued, so a worker that
    picks the id up always finds it."""
    store, sessions = await _store()
    draft_id = await _new(store, target_id="itm_a", request="something warmer")
    row = await _row(sessions, draft_id)
    assert row.status == "queued"
    assert row.content is None and row.model is None and row.error is None
    assert row.request == "something warmer"


async def test_generation_moves_it_to_drafted():
    store, sessions = await _store()
    draft_id = await _new(store)
    assert await store.complete(draft_id, content="A rich Lahori karahi.", model="fake")
    row = await _row(sessions, draft_id)
    assert row.status == "drafted"
    assert row.content == "A rich Lahori karahi."
    assert row.model == "fake"


# ── at-least-once delivery ─────────────────────────────────────────


async def test_a_redelivered_job_cannot_overwrite_a_finished_draft():
    """acks_late makes execution at-least-once. The second delivery must
    not replace copy a human may already be reading."""
    store, sessions = await _store()
    draft_id = await _new(store)
    assert await store.complete(draft_id, content="first", model="m1")
    assert not await store.complete(draft_id, content="second", model="m2")
    assert (await _row(sessions, draft_id)).content == "first"


async def test_a_late_failure_cannot_bury_a_good_draft():
    """A retry whose sibling already succeeded arriving with an error must
    not park a draft that exists."""
    store, sessions = await _store()
    draft_id = await _new(store)
    await store.complete(draft_id, content="good copy", model="m1")
    assert not await store.park(draft_id, error="provider exploded")
    row = await _row(sessions, draft_id)
    assert row.status == "drafted" and row.error is None


async def test_parking_twice_is_harmless():
    store, _ = await _store()
    draft_id = await _new(store)
    assert await store.park(draft_id, error="first")
    assert not await store.park(draft_id, error="second")


# ── parking is the dead-letter queue ───────────────────────────────


async def test_a_parked_draft_carries_a_reason_a_human_can_act_on():
    store, sessions = await _store()
    draft_id = await _new(store)
    await store.park(draft_id, error="the model declined to write this copy")
    row = await _row(sessions, draft_id)
    assert row.status == "parked"
    assert "declined" in row.error


async def test_a_very_long_error_is_truncated_rather_than_rejected():
    """An error is diagnostic text, not a payload. A provider that returns
    a page of HTML must not make the park itself fail — that would lose the
    only record that the job existed."""
    store, sessions = await _store()
    draft_id = await _new(store)
    await store.park(draft_id, error="x" * 5000)
    assert len((await _row(sessions, draft_id)).error) <= 500


async def test_replay_puts_a_parked_draft_back_in_the_queue():
    """UC-25's "replayable": an UPDATE, not a broker console."""
    store, sessions = await _store()
    draft_id = await _new(store)
    await store.park(draft_id, error="provider was down")
    await store.replay(draft_id, OWNER)
    row = await _row(sessions, draft_id)
    assert row.status == "queued"
    assert row.error is None  # the old reason would be stale on the retry


async def test_only_a_parked_draft_can_be_replayed():
    store, _ = await _store()
    draft_id = await _new(store)
    await store.complete(draft_id, content="fine", model="m")
    with pytest.raises(WrongState) as exc:
        await store.replay(draft_id, OWNER)
    assert exc.value.status == "drafted"


# ── tenancy ────────────────────────────────────────────────────────


async def test_a_rivals_draft_is_not_found_rather_than_forbidden():
    """Confirming a draft exists to a rival's token is the leak."""
    store, _ = await _store()
    draft_id = await _new(store)
    with pytest.raises(DraftNotFound):
        await store.get(draft_id, RIVAL)
    with pytest.raises(DraftNotFound):
        await store.replay(draft_id, RIVAL)


async def test_a_brand_token_reaches_its_branches_drafts():
    """A brand owns every branch (ADR-0028), and a branch's copy is the
    brand's business."""
    store, _ = await _store()
    draft_id = await _new(store, restaurant_id="rst_7", brand_id="brn_1")
    assert (await store.get(draft_id, BRAND_ONLY)).draft_id == draft_id


async def test_listing_is_scoped_in_the_where_clause():
    """A cross-tenant row is never loaded, so it cannot be leaked by a
    later bug in whatever renders the list."""
    store, _ = await _store()
    await _new(store, restaurant_id="rst_1", brand_id="brn_1")
    await _new(store, restaurant_id="rst_9", brand_id="brn_9")
    mine = await store.list_for(OWNER)
    assert [row.restaurant_id for row in mine] == ["rst_1"]


async def test_a_token_naming_neither_a_restaurant_nor_a_brand_owns_nothing():
    """The empty-claim case. Returning everything here would be exactly the
    whole-table read the scoping exists to prevent."""
    store, _ = await _store()
    await _new(store)
    assert await store.list_for(Claim(restaurant_id=None, brand_id=None)) == []


async def test_listing_can_filter_by_status_and_is_newest_first():
    store, _ = await _store()
    first = await _new(store)
    second = await _new(store)
    await store.park(first, error="broken")
    parked = await store.list_for(OWNER, status="parked")
    assert [row.draft_id for row in parked] == [first]
    assert [row.draft_id for row in await store.list_for(OWNER)][0] == second


async def test_the_worker_reads_without_a_claim():
    """A Celery task has no token — it is the system acting on a job it was
    handed. A separate method so the scoped and unscoped reads cannot be
    confused at a call site."""
    store, _ = await _store()
    draft_id = await _new(store)
    loaded = await store.load(draft_id)
    assert loaded is not None and loaded.draft_id == draft_id
    assert await store.load("cdr_nothing") is None


# ── the job, end to end over a real store ──────────────────────────


class _Gen:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.seen: list[dict] = []

    async def draft(self, *, kind, subject, request, facts=None):
        self.seen.append({"kind": kind, "subject": subject, "request": request, "facts": facts})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_the_job_drafts_and_settles_the_row():
    from ai_assistant.content import Drafted, run_draft

    store, sessions = await _store()
    draft_id = await _new(store, target_id="itm_a", request="warmer, please")
    generator = _Gen(Drafted(text="A rich Lahori karahi.", model="m1"))

    assert await run_draft(store=store, generator=generator, draft_id=draft_id) == "drafted"
    row = await _row(sessions, draft_id)
    assert row.status == "drafted" and row.content == "A rich Lahori karahi."
    # The job is written FROM the row, so the admin's words reach the model
    # exactly as typed.
    assert generator.seen == [
        {"kind": "menu_item", "subject": "itm_a", "request": "warmer, please", "facts": None}
    ]


async def test_a_job_for_a_draft_already_settled_does_nothing():
    """A sibling delivery got there first. Nothing to do, nothing wrong —
    and crucially not an error that would retry."""
    from ai_assistant.content import Drafted, run_draft

    store, _ = await _store()
    draft_id = await _new(store)
    await store.complete(draft_id, content="first", model="m1")
    generator = _Gen(Drafted(text="second", model="m2"))
    assert await run_draft(store=store, generator=generator, draft_id=draft_id) == "drafted"
    assert generator.seen == []  # never asked the model again


async def test_a_job_for_a_draft_that_does_not_exist_fails_loudly():
    """Permanent by construction — the row is committed before the job is
    enqueued. Burning a backoff schedule on a bug helps nobody."""
    from ai_assistant.content import run_draft

    store, _ = await _store()
    with pytest.raises(LookupError):
        await run_draft(store=store, generator=_Gen(None), draft_id="cdr_ghost")


async def test_a_worker_with_no_provider_parks_with_a_readable_reason():
    from ai_assistant.content import PermanentFailure, run_draft

    store, _ = await _store()
    draft_id = await _new(store)
    with pytest.raises(PermanentFailure) as exc:
        await run_draft(store=store, generator=None, draft_id=draft_id)
    assert "no model provider" in str(exc.value)


async def test_a_promotion_draft_is_written_about_the_restaurant():
    """`target_id` is null for the kinds that are about the business rather
    than one dish, and the subject falls back to the restaurant."""
    from ai_assistant.content import Drafted, run_draft

    store, _ = await _store()
    draft_id = await _new(store, kind="promotion", request="something for slow Tuesdays")
    generator = _Gen(Drafted(text="Half price Tuesdays.", model="m"))
    await run_draft(store=store, generator=generator, draft_id=draft_id)
    assert generator.seen[0]["subject"] == "rst_1"
    assert generator.seen[0]["kind"] == "promotion"


async def test_latest_returns_the_newest_of_one_kind():
    """A summary is regenerated rather than versioned, so "the summary" is
    whichever finished most recently. Older rows stay — they are the record
    of what was said about this restaurant and when."""
    store, _ = await _store()
    first = await _new(store, kind="feedback_summary")
    second = await _new(store, kind="feedback_summary")
    await _new(store, kind="menu_item")
    latest = await store.latest(OWNER, kind="feedback_summary")
    assert latest is not None and latest.draft_id == second
    assert first != second


async def test_latest_is_none_when_there_is_nothing_of_that_kind():
    store, _ = await _store()
    await _new(store, kind="menu_item")
    assert await store.latest(OWNER, kind="feedback_summary") is None


def test_every_content_task_is_routed_to_a_queue_a_worker_reads():
    """A task with no `task_routes` entry goes to Celery's DEFAULT queue,
    which no worker in this deployment consumes — so the job is accepted,
    committed, enqueued, and sits forever with no error anywhere.

    Found exactly that way: `assistant.content.summarise` was registered
    and unrouted, and the only symptom was a row stuck in `queued`.
    """
    from ai_assistant.celery_app import celery_app
    from ai_assistant.main import _TASK_FOR_KIND

    routes = celery_app.conf.task_routes
    for task in set(_TASK_FOR_KIND.values()):
        assert task in routes, task
        assert routes[task]["queue"] == "assistant.content", task


async def test_a_job_whose_completion_lost_a_race_reports_superseded():
    """Not "drafted". The rowcount was discarded, so a duplicate delivery's
    discarded answer looked exactly like a clean run — a double spend with
    no trace in any log or metric."""
    from ai_assistant.content import Drafted, run_draft

    store, _ = await _store()
    draft_id = await _new(store)
    await store.complete(draft_id, content="the winner", model="m1")

    class Settled:
        """A store whose row still looks queued to the job, so the race is
        decided by the guarded UPDATE rather than by the courtesy check."""

        def __init__(self, inner, row):
            self._inner, self._row = inner, row

        async def load(self, draft_id):
            return self._row

        async def complete(self, draft_id, **kwargs):
            return await self._inner.complete(draft_id, **kwargs)

    row = await store.load(draft_id)
    assert row is not None
    stale = type("Row", (), {**{k: getattr(row, k) for k in row._mapping}, "status": "queued"})()
    outcome = await run_draft(
        store=Settled(store, stale), generator=_Gen(Drafted("the loser", "m2")), draft_id=draft_id
    )
    assert outcome == "superseded"
    settled = await store.load(draft_id)
    assert settled is not None and settled.content == "the winner"
