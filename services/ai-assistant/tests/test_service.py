"""The use-case layer, and why admission is a separate awaited step.

A StreamingResponse's generator is only iterated after the headers have
gone out, so anything decided inside it can no longer choose a status
code. prepare() settles every refusal while a 429 or 503 is still
expressible; complete()/stream() then only do work.
"""

from typing import Any

import pytest
from ai_assistant.domain.budget import BudgetGuard, PlaneShed
from ai_assistant.domain.router import ModelRouter, NoProviderAvailable, Task, default_policy
from ai_assistant.domain.service import DIAGNOSTIC_SYSTEM, AssistantService

from .conftest import FakeBudgetStore, FakeLlm, frames

POLICY = default_policy(
    generate_model="sonnet",
    cheap_model="haiku",
    generate_fallback="gpt",
    cheap_fallback="gpt-mini",
    timeout_s=5.0,
)


def service(llm: FakeLlm | None = None, *, store: Any = None, **kwargs: Any) -> AssistantService:
    providers = {"anthropic": llm} if llm else {}
    return AssistantService(
        router=ModelRouter(providers, POLICY),
        guard=BudgetGuard(
            cell_id="c1",
            user_budget=10_000,
            user_window_s=60,
            cell_budget=100_000,
            cell_window_s=60,
            store=store,
        ),
        **kwargs,
    )


async def test_prepare_prepends_the_system_prompt_and_fits_the_prompt(llm: FakeLlm):
    turn = await service(llm).prepare(subject="usr_1", prompt="something light")
    assert [m.role for m in turn.messages] == ["system", "user"]
    assert turn.messages[0].content == DIAGNOSTIC_SYSTEM
    assert turn.task is Task.GENERATE


async def test_prepare_refuses_when_generation_is_disarmed(llm: FakeLlm):
    """`generation=off` is ladder step 2a as a manual switch, and it raises
    the SAME exception as the spend breaker — one degraded mode, one branch
    for callers to write."""
    with pytest.raises(PlaneShed):
        await service(llm, generation="off").prepare(subject="usr_1", prompt="hi")


async def test_prepare_refuses_before_spending_when_no_provider_is_registered():
    store = FakeBudgetStore()
    with pytest.raises(NoProviderAvailable):
        await service(store=store).prepare(subject="usr_1", prompt="hi")
    assert store.totals == {}  # nothing charged for a call that cannot happen


async def test_complete_settles_the_output_half_after_the_call(llm: FakeLlm):
    store = FakeBudgetStore()
    svc = service(llm, store=store)
    turn = await svc.prepare(subject="usr_1", prompt="hi")
    charged_on_admit = store.totals["assistant:budget:usr_1"]
    result = await svc.complete(turn)
    assert result.text == "a light soup"
    # admit charged the estimated input; settle charged the actual output
    assert store.totals["assistant:budget:usr_1"] == charged_on_admit + 7


async def test_stream_settles_from_the_terminal_frame(llm: FakeLlm):
    store = FakeBudgetStore()
    llm.stream_script = [frames("a ", "broth")]
    svc = service(llm, store=store)
    turn = await svc.prepare(subject="usr_1", prompt="hi")
    charged_on_admit = store.totals["assistant:budget:usr_1"]
    text = "".join([chunk.text async for chunk in svc.stream(turn)])
    assert text == "a broth"
    assert store.totals["assistant:budget:usr_1"] == charged_on_admit + 2


async def test_concurrency_cap_is_held_for_the_whole_stream(llm: FakeLlm):
    """An in-flight generation occupies provider concurrency until its last
    frame, so releasing the semaphore early would let the cap be overrun by
    exactly the number of streams still running."""
    svc = service(llm, max_concurrent_turns=1)
    turn = await svc.prepare(subject="usr_1", prompt="hi")
    iterator = svc.stream(turn)
    await iterator.__anext__()  # started, semaphore held
    assert svc._turns.locked()
    async for _ in iterator:
        pass
    assert not svc._turns.locked()
