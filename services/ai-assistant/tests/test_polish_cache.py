"""The rewrite layer over the template floor (FR-87).

What matters here is not that a rewrite happens — it is that everything
which can go wrong costs the customer nothing, and costs the system at most
one attempt per situation.
"""

from typing import cast

import pytest
from ai_assistant.domain.explain import ReasonCode
from ai_assistant.domain.ports import Completion, FinishReason, LlmRateLimited, LlmUnavailable
from ai_assistant.domain.render import Bucket, TemplateCache
from ai_assistant.polish_cache import Completes, PolishedTemplates

KEY = (ReasonCode.AWAITING_COURIER, "en", Bucket.SHORT)


class FakeRouter:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def complete(self, task, messages):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.outcomes
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion(text: str, finish_reason: FinishReason = "stop") -> Completion:
    return Completion(
        text=text,
        finish_reason=finish_reason,
        model="fake",
        prompt_tokens=1,
        completion_tokens=1,
        provider="fake",
    )


def _cache(*outcomes) -> tuple[PolishedTemplates, FakeRouter]:
    router = FakeRouter(*outcomes)
    return PolishedTemplates(TemplateCache(), router=cast(Completes, router)), router


async def test_a_good_rewrite_replaces_the_template():
    better = "Your meal has been ready for {elapsed} while we look for a courier."
    cache, _ = _cache(_completion(better))
    before = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == better
    assert cache.get(*KEY) != before
    assert cache.polished(*KEY)


async def test_a_rewrite_that_invents_a_number_is_discarded():
    cache, _ = _cache(_completion("Ready for {elapsed}; a courier arrives in 10 minutes."))
    original = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == original
    assert not cache.polished(*KEY)


async def test_a_truncated_rewrite_is_discarded():
    """`length` means the model ran out of tokens mid-sentence."""
    cache, _ = _cache(_completion("Your food has been ready for {elapsed} and we", "length"))
    original = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == original


async def test_a_refusal_is_discarded():
    cache, _ = _cache(_completion("I can't help with that.", "refusal"))
    original = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == original


async def test_an_unavailable_model_costs_nothing():
    cache, _ = _cache(LlmUnavailable("gemini is down"))
    original = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == original


async def test_an_unexpected_error_is_swallowed_too():
    """This runs after the customer already has their answer. There is no
    failure here worth turning into a visible one."""
    cache, _ = _cache(RuntimeError("something nobody predicted"))
    original = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == original


async def test_a_failed_key_is_never_retried():
    """The guard that keeps a polling order page from paying a provider
    timeout every fifteen seconds for as long as the model stays unhappy."""
    cache, router = _cache(LlmRateLimited("slow down"))
    for _ in range(5):
        await cache.warm(*KEY)
    assert router.calls == 1


async def test_a_succeeded_key_is_never_re_asked():
    cache, router = _cache(_completion("Ready for {elapsed}; still looking for a courier."))
    for _ in range(5):
        await cache.warm(*KEY)
    assert router.calls == 1


async def test_with_no_router_nothing_is_attempted():
    """FR-87's case. Every explanation is answered from the template and
    this layer is inert — not disabled by a flag, just never asked."""
    cache = PolishedTemplates(TemplateCache(), router=None)
    original = cache.get(*KEY)
    await cache.warm(*KEY)
    assert cache.get(*KEY) == original
    assert not cache.polished(*KEY)


async def test_a_key_with_no_copy_is_not_sent_to_a_model():
    cache, router = _cache(_completion("anything"))
    await cache.warm(ReasonCode.DELIVERED, "en", Bucket.SHORT)
    # DELIVERED has a default template, so that one IS asked; a cache with
    # no copy at all must not be.
    empty = PolishedTemplates(TemplateCache({"en": {}}), router=cast(Completes, router))
    calls = router.calls
    await empty.warm(*KEY)
    assert router.calls == calls


async def test_rewrites_are_per_bucket_not_per_reason():
    """The same cause reads differently over time, so the copy differs per
    bucket — a rewrite of one must not be served for another."""
    better = "Your meal has been ready for {elapsed}; still looking for a courier."
    cache, _ = _cache(_completion(better))
    await cache.warm(*KEY)
    assert cache.get(*KEY) == better
    assert cache.get(ReasonCode.AWAITING_COURIER, "en", Bucket.JUST_NOW) != better


async def test_a_cancelled_warm_gives_the_key_its_attempt_back():
    """`CancelledError` derives from BaseException, so it sailed past the
    catch-all and left the key claimed-as-failed forever — on every
    shutdown, for every in-flight warm."""
    import asyncio

    started = asyncio.Event()

    class Hanging:
        async def complete(self, task, messages):
            started.set()
            await asyncio.Event().wait()  # never resolves

    cache = PolishedTemplates(TemplateCache(), router=cast(Completes, Hanging()))
    task = asyncio.create_task(cache.warm(*KEY))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Claimed during the attempt, released by the cancellation.
    assert KEY not in cache._rewritten  # noqa: SLF001
    assert not cache.polished(*KEY)


async def test_a_shed_keeps_the_template_without_burning_the_key():
    """`generation=off` is a reversible DECISION, so it must not consume the
    key's one attempt the way a model failure does. Otherwise throwing the
    switch during a spend incident leaves every explanation unpolished until
    the process restarts — long after the switch went back on.

    The same reasoning the CancelledError branch already applies, now that
    the shed is enforced on the router and therefore reaches this path."""
    from ai_assistant.domain.ports import PlaneShed

    cache, router = _cache(PlaneShed("generation disarmed (ladder step 2a)"))
    template = cache.get(*KEY)

    await cache.warm(*KEY)
    assert cache.get(*KEY) == template  # the customer still gets a real answer
    assert not cache.polished(*KEY)

    # The switch goes back on: the key was never claimed, so the next warm
    # actually tries again.
    better = "Your meal has been ready for {elapsed} while we look for a courier."
    router.outcomes.append(_completion(better))
    await cache.warm(*KEY)
    assert cache.get(*KEY) == better
