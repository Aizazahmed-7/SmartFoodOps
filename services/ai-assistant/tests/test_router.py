"""Task->model routing and cross-vendor failover (ADR-0030 §3-§4).

The rule this file exists to defend: a streamed answer fails over ONLY
before its first token. After that, switching providers would splice two
models' prose together mid-sentence, which is worse than a visible failure.
"""

import pytest
from ai_assistant.domain.ports import LlmRateLimited, LlmUnavailable, Message, TokenChunk
from ai_assistant.domain.router import (
    ModelRouter,
    NoProviderAvailable,
    Task,
    default_policy,
)

from .conftest import FakeLlm, completion, frames

POLICY = default_policy(
    generate_model="sonnet",
    cheap_model="haiku",
    generate_fallback="gpt",
    cheap_fallback="gpt-mini",
    timeout_s=5.0,
)
ASK = [Message("user", "something light")]


def router(**providers: FakeLlm) -> ModelRouter:
    return ModelRouter(providers, POLICY)


async def drain(iterator) -> list[TokenChunk]:
    return [chunk async for chunk in iterator]


def test_policy_sends_cheap_work_to_the_cheap_tier():
    assert POLICY[Task.CLASSIFY].primary.model == "haiku"
    assert POLICY[Task.GENERATE].primary.model == "sonnet"
    # Batch tasks have no secondary: no user is waiting, so a retry later
    # beats paying a second vendor now.
    assert POLICY[Task.SUMMARIZE].secondary is None


def test_specs_for_skips_unregistered_providers():
    only_openai = router(openai=FakeLlm("openai"))
    specs = only_openai.specs_for(Task.GENERATE)
    assert [spec.provider for spec in specs] == ["openai"]


def test_specs_for_is_empty_when_no_key_is_configured():
    assert router().specs_for(Task.GENERATE) == []


async def test_complete_uses_the_primary_and_counts_tokens():
    anthropic = FakeLlm("anthropic")
    result = await router(anthropic=anthropic).complete(Task.GENERATE, ASK)
    assert result.text == "a light soup"
    assert anthropic.calls[0][1] == "sonnet"


async def test_complete_fails_over_on_rate_limit():
    anthropic = FakeLlm("anthropic")
    anthropic.script = [LlmRateLimited("tpm exhausted")]
    openai = FakeLlm("openai")
    openai.script = [completion("a broth", provider="openai")]
    result = await router(anthropic=anthropic, openai=openai).complete(Task.GENERATE, ASK)
    assert result.provider == "openai"
    assert openai.calls[0][1] == "gpt"


async def test_complete_fails_over_on_unavailable_too():
    anthropic = FakeLlm("anthropic")
    anthropic.script = [LlmUnavailable("connection reset")]
    openai = FakeLlm("openai")
    result = await router(anthropic=anthropic, openai=openai).complete(Task.GENERATE, ASK)
    assert result.text == "a light soup"


async def test_complete_raises_when_every_provider_fails():
    anthropic = FakeLlm("anthropic")
    anthropic.script = [LlmUnavailable("down")]
    openai = FakeLlm("openai")
    openai.script = [LlmUnavailable("also down")]
    with pytest.raises(NoProviderAvailable):
        await router(anthropic=anthropic, openai=openai).complete(Task.GENERATE, ASK)


async def test_complete_raises_when_nothing_is_registered():
    with pytest.raises(NoProviderAvailable):
        await router().complete(Task.GENERATE, ASK)


async def test_complete_tolerates_a_provider_reporting_no_usage():
    """Zero-token usage is a real provider response, not an error — the
    counter guards exist so it does not emit a meaningless zero sample."""
    anthropic = FakeLlm("anthropic")
    anthropic.script = [completion(prompt_tokens=0, completion_tokens=0)]
    result = await router(anthropic=anthropic).complete(Task.GENERATE, ASK)
    assert result.prompt_tokens == 0


async def test_stream_yields_deltas_then_the_terminal_usage_frame():
    anthropic = FakeLlm("anthropic")
    anthropic.stream_script = [frames("a ", "light ", "soup")]
    chunks = await drain(router(anthropic=anthropic).stream(Task.GENERATE, ASK))
    assert "".join(chunk.text for chunk in chunks) == "a light soup"
    assert chunks[-1].done and chunks[-1].completion_tokens == 3


async def test_stream_fails_over_before_the_first_token():
    anthropic = FakeLlm("anthropic")
    anthropic.stream_script = [[LlmUnavailable("handshake failed")]]
    openai = FakeLlm("openai")
    openai.stream_script = [frames("broth")]
    chunks = await drain(router(anthropic=anthropic, openai=openai).stream(Task.GENERATE, ASK))
    assert "".join(chunk.text for chunk in chunks) == "broth"


async def test_stream_does_not_fail_over_after_a_token_was_delivered():
    """The crux. Two models' half-answers spliced together would be worse
    than an error the user can retry."""
    anthropic = FakeLlm("anthropic")
    anthropic.stream_script = [[TokenChunk(text="a li"), LlmUnavailable("reset mid-answer")]]
    openai = FakeLlm("openai")
    openai.stream_script = [frames("completely different answer")]
    iterator = router(anthropic=anthropic, openai=openai).stream(Task.GENERATE, ASK)
    seen: list[str] = []
    with pytest.raises(LlmUnavailable):
        async for chunk in iterator:
            seen.append(chunk.text)
    assert seen == ["a li"]
    assert openai.calls == []  # the secondary was never asked


async def test_stream_raises_when_every_provider_fails_the_handshake():
    anthropic = FakeLlm("anthropic")
    anthropic.stream_script = [[LlmRateLimited("tpm")]]
    openai = FakeLlm("openai")
    openai.stream_script = [[LlmUnavailable("down")]]
    with pytest.raises(NoProviderAvailable):
        await drain(router(anthropic=anthropic, openai=openai).stream(Task.GENERATE, ASK))


async def test_stream_raises_when_nothing_is_registered():
    with pytest.raises(NoProviderAvailable):
        await drain(router().stream(Task.GENERATE, ASK))


async def test_stream_on_a_single_provider_task_does_not_record_a_failover():
    """CONTENT_DRAFT has no secondary, so the last-candidate branch must
    not try to label a failover that has no destination."""
    anthropic = FakeLlm("anthropic")
    anthropic.stream_script = [[LlmUnavailable("down")]]
    with pytest.raises(NoProviderAvailable):
        await drain(router(anthropic=anthropic).stream(Task.CONTENT_DRAFT, ASK))


def test_router_refuses_a_provider_registered_under_the_wrong_name():
    """A wiring bug that would otherwise show up only as a confusing cost
    report: every anthropic-routed task quietly served by OpenAI."""
    with pytest.raises(ValueError, match="identifies as 'openai'"):
        ModelRouter({"anthropic": FakeLlm("openai")}, POLICY)
