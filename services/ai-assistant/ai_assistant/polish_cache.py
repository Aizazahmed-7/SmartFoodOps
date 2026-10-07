"""The model-rewritten half of FR-87's template cache.

`TemplateCache` holds the copy that ships. This wraps it and, for each
`(reason_code, locale, bucket)`, may hold a rewritten variant a model
produced once. `render()` is untouched — it asks a cache for a template and
does not care which layer answered, which is the point of putting the
rewrite here rather than in the renderer.

**One attempt per key, ever, per process.** A key that fails — the model is
down, the rewrite broke a guard, no provider is configured — is remembered
as failed and never retried. Without that, a customer's order page polling
every fifteen seconds would pay a provider timeout on every poll for as
long as the model stayed unhappy. With it, the worst case is one attempt
per situation the app can be in, and every subsequent reader gets a cached
answer or the original copy at full speed.

**In-process, not Redis.** Each replica therefore pays its own attempt, and
a deploy forgets everything learned. That is the honest trade for now: the
population is tiny (a couple of dozen live keys), the fallback is copy that
was already good, and a shared store would add an availability dependency
to the one path in this milestone that currently has none. Moving it to
Redis is a store swap behind this class, not a redesign.
"""

import asyncio
from collections.abc import Sequence
from typing import Protocol

from smartfood_otel import get_logger

from ai_assistant.domain.explain import ReasonCode
from ai_assistant.domain.polish import SYSTEM_PROMPT, prompt_for, review
from ai_assistant.domain.ports import (
    Completion,
    LlmRateLimited,
    LlmUnavailable,
    Message,
    PlaneShed,
)
from ai_assistant.domain.render import Bucket, TemplateCache
from ai_assistant.domain.router import Task

log = get_logger("ai-assistant.polish")

Key = tuple[ReasonCode, str, Bucket]


class Completes(Protocol):
    """Just the one call this layer makes.

    Narrower than `LlmPort` on purpose, and a different shape: `LlmPort` is
    a provider (it takes a model name), while what this needs is the
    ROUTER — give it a task and let the policy choose the model and the
    failover. Depending on the wide type here would have typechecked
    against a provider and then failed at runtime on the argument names.
    """

    async def complete(self, task: Task, messages: Sequence[Message]) -> Completion: ...


class PolishedTemplates(TemplateCache):
    """A TemplateCache that may answer with a rewrite.

    Subclassing rather than wrapping so it can be passed anywhere a
    `TemplateCache` goes — including into `render()`, which stays a pure
    function over a lookup it does not need to understand.
    """

    def __init__(self, base: TemplateCache, *, router: Completes | None = None) -> None:
        super().__init__(base._templates)  # noqa: SLF001 — same class, same field
        self._base = base
        self._router = router
        self._rewritten: dict[Key, str | None] = {}

    def get(self, reason: ReasonCode, locale: str, bucket: Bucket) -> str | None:
        rewritten = self._rewritten.get((reason, locale, bucket))
        return rewritten if rewritten is not None else self._base.get(reason, locale, bucket)

    def polished(self, reason: ReasonCode, locale: str, bucket: Bucket) -> bool:
        """Whether a model wrote the words this key will answer with — the
        `source` a stored explanation reports, so support can tell."""
        return self._rewritten.get((reason, locale, bucket)) is not None

    async def warm(self, reason: ReasonCode, locale: str, bucket: Bucket) -> None:
        """Try, once, to improve one key. Never raises.

        Called after the customer already has their answer, so the cost of
        the attempt is never charged to the explanation it might improve.
        """
        key = (reason, locale, bucket)
        if self._router is None or key in self._rewritten:
            return
        original = self._base.get(reason, locale, bucket)
        if original is None:
            return
        self._rewritten[key] = None  # claimed: a failure below must not retry

        try:
            completion = await self._router.complete(
                Task.EXPLAIN,
                [
                    Message(role="system", content=SYSTEM_PROMPT),
                    Message(role="user", content=prompt_for(reason, original)),
                ],
            )
        except asyncio.CancelledError:
            # Not a failure of the model, so it must not burn the key's one
            # attempt. `CancelledError` derives from BaseException and would
            # otherwise sail past the catch below, leaving the key claimed
            # as failed forever — on every shutdown, for every in-flight
            # warm.
            self._rewritten.pop(key, None)
            raise
        except PlaneShed:
            # Same reasoning as CancelledError above: a shed is a DECISION,
            # and a reversible one. Burning the key would mean that throwing
            # the switch during a spend incident leaves every explanation
            # unpolished until the process restarts, long after the switch
            # went back on.
            self._rewritten.pop(key, None)
            log.info("explanation rewrite shed — keeping the template")
            return
        except (LlmUnavailable, LlmRateLimited, Exception) as exc:  # noqa: BLE001
            # Deliberately everything: this runs after the answer has been
            # produced, and there is no failure here worth turning into a
            # customer-visible one.
            log.info("explanation rewrite unavailable — keeping the template", error=str(exc))
            return

        if completion.finish_reason != "stop":
            # `length` means a truncated sentence and `refusal` means the
            # model declined; neither is copy to show anyone.
            return
        verdict = review(original, completion.text)
        if verdict.kept is None:
            # The candidate is logged with it. This copy is a template with
            # its placeholders unfilled — no customer, no order, no PII —
            # and without it "rejected" is a number nobody can act on.
            log.info(
                "explanation rewrite rejected — keeping the template",
                reason=reason.value,
                bucket=bucket.value,
                rule=verdict.rule,
                candidate=completion.text.strip()[:200],
            )
            return
        self._rewritten[key] = verdict.kept
