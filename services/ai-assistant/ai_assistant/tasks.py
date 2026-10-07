"""The AI plane's worker half. B1: the rolling reindex (FR-61).

The task is a thin shell over `run_reindex` — build the async wiring, run
it, return the summary. Everything worth testing is in `reindex.py`, which
needs no broker and no Celery to exercise.

Idempotency (execution is at-least-once by config — acks_late): the reindex
resumes from an anti-join, so a redelivered task migrates whatever is left
rather than redoing what is done. Re-running it is both the retry policy and
the completion procedure.
"""

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .celery_app import celery_app
from .config import Settings
from .content import PermanentFailure, run_draft, run_summary
from .domain.ports import LlmRateLimited, LlmUnavailable
from .providers import shed_reason
from .reindex import run_reindex


async def _run(settings: Settings) -> dict[str, Any]:
    from .adapters.embeddings_fake import FakeEmbeddings

    engine = create_async_engine(settings.database_url)
    try:
        embeddings = FakeEmbeddings(dimensions=settings.embedding_dimensions)
        if settings.openai_api_key:  # pragma: no cover — live wiring
            import httpx

            from .adapters.embeddings_openai import OpenAiEmbeddings

            async with httpx.AsyncClient() as http:
                result = await run_reindex(
                    async_sessionmaker(engine, expire_on_commit=False),
                    OpenAiEmbeddings(
                        api_key=settings.openai_api_key,
                        base_url=settings.openai_base_url,
                        http=http,
                        model=settings.embedding_model,
                        dimensions=settings.embedding_dimensions,
                    ),
                    batch=settings.reindex_batch,
                    max_batches=settings.reindex_max_batches,
                )
                return vars(result)
        result = await run_reindex(
            async_sessionmaker(engine, expire_on_commit=False),
            embeddings,
            batch=settings.reindex_batch,
            max_batches=settings.reindex_max_batches,
        )
        return vars(result)
    finally:
        await engine.dispose()


@celery_app.task(name="assistant.reindex")
def reindex() -> dict[str, Any]:  # pragma: no cover — the shell; run_reindex is tested
    """Migrate the corpus toward the configured model version.

    Returns the `ReindexResult` as a dict. `done: false` means the batch cap
    was reached and there is more to do — re-run it.
    """
    return asyncio.run(_run(Settings()))


# ── B6: the content studio (FR-88, UC-25) ───────────────────────────


async def _draft(settings: Settings, draft_id: str) -> str:  # pragma: no cover — wiring
    from .drafts import DraftStore
    from .providers import build_generator

    engine = create_async_engine(settings.database_url)
    try:
        store = DraftStore(async_sessionmaker(engine, expire_on_commit=False))
        return await run_draft(store=store, generator=build_generator(settings), draft_id=draft_id)
    finally:
        await engine.dispose()


async def _park(settings: Settings, draft_id: str, error: str) -> None:  # pragma: no cover
    from .drafts import DraftStore

    engine = create_async_engine(settings.database_url)
    try:
        await DraftStore(async_sessionmaker(engine, expire_on_commit=False)).park(
            draft_id, error=error
        )
    finally:
        await engine.dispose()


async def _summarise(settings: Settings, draft_id: str) -> str:  # pragma: no cover — wiring
    import httpx

    from .adapters.order_client import FeedbackClient
    from .adapters.summariser import FeedbackSummariser
    from .drafts import DraftStore
    from .providers import build_router

    engine = create_async_engine(settings.database_url)
    http = httpx.AsyncClient(timeout=5.0)
    try:
        router = build_router(settings)
        return await run_summary(
            store=DraftStore(async_sessionmaker(engine, expire_on_commit=False)),
            summariser=FeedbackSummariser(router) if router is not None else None,
            feedback=FeedbackClient(settings.order_base_url, http),
            draft_id=draft_id,
        )
    finally:
        await http.aclose()
        await engine.dispose()


def _run_job(task, draft_id: str, body) -> str:  # pragma: no cover — the shell
    """Run one content job, and leave NOTHING in `queued`.

    Three arms, and the third is the one an adversarial pass found missing:

    - the provider having a bad minute retries, and parks once the schedule
      is exhausted so an outage that outlasts it is still visible;
    - a `PermanentFailure` parks immediately;
    - **anything else parks too.** An Order outage raising
      `UpstreamUnavailable` from the feedback read, a database blip, a bug
      — none of those were caught, so Celery marked the task failed, acked
      it, and the row stayed `queued` forever. A `queued` row is reachable
      by no human action: approve and reject need `drafted`, replay needs
      `parked`. It was a dead end with a spinner on it, and the console
      polled it every four seconds for the life of the page.
    """
    settings = Settings()
    shed = shed_reason(settings)
    if shed is not None:
        # Before the body, so no provider call is made and the shed is real
        # spend avoided rather than a bill followed by a failure.
        asyncio.run(_park(settings, draft_id, shed))
        return "parked"
    try:
        return asyncio.run(body(settings, draft_id))
    except PermanentFailure as exc:
        asyncio.run(_park(settings, draft_id, str(exc)))
        return "parked"
    except (LlmUnavailable, LlmRateLimited) as exc:
        if task.request.retries >= task.max_retries:
            asyncio.run(_park(settings, draft_id, f"provider unavailable: {exc}"))
            return "parked"
        raise
    except Exception as exc:  # noqa: BLE001 — see the docstring
        asyncio.run(_park(settings, draft_id, f"the job failed unexpectedly: {exc}"))
        return "parked"


@celery_app.task(
    name="assistant.content.draft",
    bind=True,
    autoretry_for=(LlmUnavailable, LlmRateLimited),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=4,
    acks_late=True,
)
def draft_content(self, draft_id: str) -> str:  # pragma: no cover — the shell
    """Write one draft (FR-88/89/90).

    Two failure arms and no third:

    - `LlmUnavailable` / `LlmRateLimited` are the provider having a bad
      minute. `autoretry_for` backs off; parking here would throw away a
      draft that was going to succeed.
    - `PermanentFailure` is a request that will never work — a refusal, a
      guard rejection, a missing subject. It parks: visible to the
      restaurant, replayable by a human, and not burning four backoffs to
      reach the same answer.

    A retry that exhausts `max_retries` parks too, with the provider's own
    message, so an outage that outlasts the schedule is still visible in
    the console rather than only in a worker log.
    """
    return _run_job(self, draft_id, _draft)


@celery_app.task(
    name="assistant.content.summarise",
    bind=True,
    autoretry_for=(LlmUnavailable, LlmRateLimited),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=4,
    acks_late=True,
)
def summarise_feedback(self, draft_id: str) -> str:  # pragma: no cover — the shell
    """Summarise one restaurant's reviews (FR-92).

    Same two failure arms as the draft task: a provider having a bad minute
    retries, and everything that will never work parks — including a
    summary whose quotes could not be found in the corpus, which is the
    one failure this feature exists to prevent.
    """
    return _run_job(self, draft_id, _summarise)
