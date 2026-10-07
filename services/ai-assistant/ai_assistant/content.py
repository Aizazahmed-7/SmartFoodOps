"""Generating one draft, and deciding what to do when it fails (UC-25).

The task body is thin on purpose: everything worth testing lives here and
needs no broker. What this module owns is the one judgement the queue
cannot make for itself — **is this failure worth retrying?**

Retryable and permanent are not the same shape of problem:

- A provider that is down or rate-limiting will probably work in a minute.
  Celery's backoff exists for exactly this, and parking would throw away a
  draft that was going to succeed.
- A refusal, a truncation, a restaurant that no longer has the item, a
  prompt the guard rejects — none of these improve by waiting. Retrying
  them burns a backoff schedule to arrive at the same answer, and the
  restaurant's copy is late for no reason.

The second group parks: visible to the restaurant, with a sentence an
operator can act on, and replayable with an UPDATE.
"""

import json
from dataclasses import dataclass

from smartfood_otel import get_logger

from .domain.claims import unsupportable
from .domain.ports import LlmRateLimited, LlmUnavailable
from .domain.summaries import MIN_ROWS

log = get_logger("ai-assistant.content")


class PermanentFailure(Exception):
    """Retrying cannot fix this. The task parks instead of backing off.

    Carries the sentence an operator reads in the console, so the reason a
    draft never arrived is in the row rather than in a log nobody correlates.
    """


@dataclass(frozen=True)
class Drafted:
    text: str
    model: str


async def generate(
    *,
    generator,
    kind: str,
    subject: str,
    request: str | None,
    facts: dict | None = None,
) -> Drafted:
    """Produce one draft, or raise.

    `LlmUnavailable` and `LlmRateLimited` propagate unchanged — the task
    retries those. Everything else that means "this will never work" is
    converted to `PermanentFailure` here, so the task's two `except` arms
    are the whole policy and there is no third case to forget.
    """
    try:
        drafted = await generator.draft(kind=kind, subject=subject, request=request, facts=facts)
    except (LlmUnavailable, LlmRateLimited):
        raise  # retryable: the provider, not the request
    except PermanentFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        # An unexpected failure is permanent until a human has looked at
        # it. Retrying an unknown error six times mostly produces six
        # identical stack traces and a draft that is an hour late.
        raise PermanentFailure(f"draft generation failed: {exc}") from exc

    if drafted is None:
        raise PermanentFailure("the model declined to write this copy")

    # FR-90's two named failures, checked after the model and before the
    # row. Permanent rather than retryable: a model that reached for a
    # testimonial once will reach for one again, and four backoffs produce
    # four testimonials. The admin sees the rule in the console and can
    # re-ask with a different instruction.
    claim = unsupportable(drafted.text)
    if claim is not None:
        log.info(
            "draft rejected — unsupportable claim",
            kind=kind,
            rule=claim.rule,
            matched=claim.matched,
        )
        raise PermanentFailure(
            f"the copy claimed something we cannot support ({claim.rule}): {claim.matched!r}"
        )
    return drafted


async def run_draft(*, store, generator, draft_id: str) -> str:
    """Generate one draft and settle its row. Raises for the task to catch.

    Lives here rather than in `tasks.py` for the reason `run_reindex`
    does: everything worth testing should need no broker and no Celery to
    exercise, leaving the task as a shell that builds wiring and calls this.

    The `queued` check is a courtesy, not the guard. The guard is in
    `store.complete`, which is conditional on the same status — execution
    is at-least-once, and two deliveries can pass this check concurrently.
    """
    row = await store.load(draft_id)
    if row is None:
        # Permanent by construction: the row is committed before the job is
        # enqueued and rows are never deleted. Not retryable — this is a
        # bug, and it should fail loudly rather than burn a backoff.
        raise LookupError(f"no draft row for {draft_id}")
    if row.status != "queued":
        # Settled by a sibling delivery, or already decided by a human.
        # Nothing to do, and nothing wrong.
        return row.status
    if generator is None:
        raise PermanentFailure("no model provider is configured for drafting")

    drafted = await generate(
        generator=generator,
        kind=row.kind,
        subject=row.target_id or row.restaurant_id,
        request=row.request,
        # Frozen when the admin asked, so the copy describes what they were
        # looking at rather than racing a later menu edit.
        facts=row.subject,
    )
    if not await store.complete(draft_id, content=drafted.text, model=drafted.model):
        # A duplicate delivery or a replay got there first. Harmless to the
        # row and NOT harmless to the bill — this used to report "drafted"
        # either way, so a double-spend was indistinguishable from a clean
        # run in every log and metric.
        log.info("draft completion lost a race — discarding this answer", draft_id=draft_id)
        return "superseded"
    return "drafted"


async def run_summary(*, store, summariser, feedback, draft_id: str) -> str:
    """Summarise one restaurant's feedback (FR-92).

    The same shape as `run_draft` and for the same reasons: no broker, no
    Celery, and the `queued` guard belongs to the store because execution
    is at-least-once.

    The corpus is re-read HERE rather than frozen onto the row the way menu
    facts are. The difference is what the two are for: a menu draft
    describes a dish as it was when the admin asked, and a feedback summary
    describes what customers are saying now. A summary computed from a
    week-old corpus would be a summary of a week-old restaurant.
    """
    from .domain.summaries import Rejected, review

    row = await store.load(draft_id)
    if row is None:
        raise LookupError(f"no draft row for {draft_id}")
    if row.status != "queued":
        return row.status
    if summariser is None:
        raise PermanentFailure("no model provider is configured for summarising")

    rows = await feedback.for_restaurant(row.restaurant_id)
    comments = [r.comment for r in rows if (r.comment or "").strip()]
    if len(comments) < MIN_ROWS:
        # The corpus shrank between the request and the job — a review
        # deleted, or an admin who asked twice. Parking says so rather than
        # summarising three opinions.
        raise PermanentFailure(f"only {len(comments)} reviews with comments; {MIN_ROWS} are needed")

    raw = await summariser.summarise([c for c in comments if c])
    if raw is None:
        raise PermanentFailure("the model did not return a readable summary")

    verdict = review(themes=raw.themes, quotes=raw.quotes, comments=[c for c in comments if c])
    if isinstance(verdict, Rejected):
        # The rule UC-28 exists for. A quote nobody said is a fabricated
        # testimonial attributed to a real business's real customer.
        log.info("summary rejected", draft_id=draft_id, rule=verdict.rule, detail=verdict.detail)
        raise PermanentFailure(f"the summary could not be verified ({verdict.rule})")

    if not await store.complete(
        draft_id,
        content=json.dumps({"themes": verdict.themes, "quotes": verdict.quotes}),
        model=raw.model,
    ):
        log.info("summary completion lost a race — discarding this answer", draft_id=draft_id)
        return "superseded"
    return "drafted"
