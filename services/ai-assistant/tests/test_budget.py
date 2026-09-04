"""The budget guard's three limits, and the trimming rules that keep a
prompt inside the per-request cap without dropping the question."""

from typing import Any

import pytest
from ai_assistant.domain.budget import BudgetExceeded, BudgetGuard, PlaneShed, estimate_tokens
from ai_assistant.domain.ports import Message
from ai_assistant.domain.router import ModelSpec, Route, Task

from .conftest import FakeBudgetStore

SPEC = ModelSpec("anthropic", "m", max_output_tokens=64, timeout_s=1.0)


def route(cap: int) -> Route:
    return Route(Task.GENERATE, cap, SPEC)


def guard(**kwargs: Any) -> BudgetGuard:
    base: dict[str, Any] = dict(
        cell_id="c1",
        user_budget=1_000,
        user_window_s=60,
        cell_budget=10_000,
        cell_window_s=60,
    )
    base.update(kwargs)
    return BudgetGuard(**base)


def test_estimate_is_never_zero_for_nonempty_text():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


def test_fit_keeps_everything_when_it_fits():
    messages = [Message("system", "s"), Message("user", "hello")]
    fitted, tokens = guard().fit(route(1_000), messages)
    assert fitted == messages
    assert tokens == 3  # "s" -> 1, "hello" -> 2


def test_fit_drops_oldest_history_and_stops_at_the_first_misfit():
    """History must stay CONTIGUOUS — skipping a middle turn to squeeze in
    an older one produces context that reads as a non-sequitur."""
    messages = [
        Message("system", "s"),
        Message("user", "a" * 400),  # oldest, 100 tokens
        Message("assistant", "b" * 40),  # 10 tokens
        Message("user", "c" * 40),  # newest, 10 tokens
    ]
    fitted, tokens = guard().fit(route(25), messages)
    assert [m.role for m in fitted] == ["system", "assistant", "user"]
    assert fitted[-1].content.startswith("c")
    assert tokens <= 25


def test_fit_never_drops_the_newest_turn_only_clips_it():
    messages = [Message("system", "s"), Message("user", "q" * 4000)]
    fitted, tokens = guard().fit(route(10), messages)
    assert len(fitted) == 2
    assert fitted[1].role == "user"
    assert 0 < len(fitted[1].content) < 4000
    assert tokens <= 10


def test_clip_loops_when_the_first_guess_still_overshoots():
    """A real tokenizer is not linear in characters, and the estimator is
    injectable for exactly that reason. A concave estimator makes the
    ratio-based first guess overshoot, exercising the refinement loop."""
    concave = BudgetGuard(
        cell_id="c1",
        user_budget=1,
        user_window_s=1,
        cell_budget=1,
        cell_window_s=1,
        estimate=lambda text: max(1, int(len(text) ** 0.5)),
    )
    fitted, _ = concave.fit(route(6), [Message("user", "z" * 100)])
    assert concave._estimate(fitted[0].content) <= 6


def test_fit_refuses_a_system_prompt_that_alone_exceeds_the_cap():
    with pytest.raises(ValueError, match="system prompt alone exceeds"):
        guard().fit(route(2), [Message("system", "s" * 400), Message("user", "q")])


async def test_admit_without_a_store_still_applies_the_request_cap():
    """Local-dev mode: the per-request cap needs no store, the shared
    limits do. Correct for tests, never correct in deployment."""
    fitted = await guard().admit(
        route=route(1_000), subject="usr_1", messages=[Message("user", "hi")]
    )
    assert fitted == [Message("user", "hi")]


async def test_admit_charges_cell_then_user(store: FakeBudgetStore):
    await guard(store=store).admit(
        route=route(1_000), subject="usr_1", messages=[Message("user", "hi")]
    )
    assert list(store.totals) == ["assistant:cb:c1", "assistant:budget:usr_1"]
    assert store.windows["assistant:cb:c1"] == 60


async def test_admit_raises_plane_shed_when_the_cell_breaker_is_open(store: FakeBudgetStore):
    store.refuse.add("assistant:cb:c1")
    with pytest.raises(PlaneShed):
        await guard(store=store).admit(
            route=route(1_000), subject="usr_1", messages=[Message("user", "hi")]
        )
    # The user's budget was never touched: if the plane is shedding, nobody
    # is served, so there is nothing to charge them for.
    assert "assistant:budget:usr_1" not in store.totals


async def test_admit_raises_budget_exceeded_for_one_subject(store: FakeBudgetStore):
    store.refuse.add("assistant:budget:usr_1")
    with pytest.raises(BudgetExceeded):
        await guard(store=store).admit(
            route=route(1_000), subject="usr_1", messages=[Message("user", "hi")]
        )


async def test_settle_charges_the_output_half(store: FakeBudgetStore):
    await guard(store=store).settle(subject="usr_1", tokens=42)
    assert store.totals["assistant:budget:usr_1"] == 42
    assert store.totals["assistant:cb:c1"] == 42


async def test_settle_is_a_no_op_without_a_store_or_without_tokens(store: FakeBudgetStore):
    await guard().settle(subject="usr_1", tokens=42)
    await guard(store=store).settle(subject="usr_1", tokens=0)
    assert store.totals == {}
