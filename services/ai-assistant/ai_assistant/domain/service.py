"""The assistant use-case layer.

Thin by design in B0: it composes the budget guard and the router, holds
the generation concurrency cap, and exposes the diagnostic entry points
that prove a provider is reachable and streaming. The LangGraph turn
(domain/graph/) lands in B3 and enters through the same seams.

Nothing here imports fastapi and nothing here does I/O of its own, so the
whole layer runs headless against scripted fakes — which is what makes the
100% gate reachable over a non-deterministic dependency.

**Why `prepare()` is separate from `stream()`.** A StreamingResponse's
generator is only iterated AFTER the response headers have gone out, so
anything decided inside it can no longer choose a status code — a budget
refusal would have to be reported as an error frame on a 200. Admission is
therefore a distinct, awaited step the route completes first: shed, budget
and no-provider are all settled while a 429 or 503 is still expressible.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal

from .budget import BudgetGuard, PlaneShed
from .ports import Completion, Message, TokenChunk
from .router import ModelRouter, NoProviderAvailable, Task

DIAGNOSTIC_SYSTEM = (
    "You are a diagnostic echo for the SmartFoodOps assistant plane. Answer briefly and literally."
)


@dataclass(frozen=True)
class Turn:
    """An admitted, fitted, ready-to-run model call. Holding the fitted
    messages means both providers see the IDENTICAL prompt on failover."""

    task: Task
    subject: str
    messages: Sequence[Message]


class AssistantService:
    def __init__(
        self,
        *,
        router: ModelRouter,
        guard: BudgetGuard,
        generation: Literal["on", "off"] = "on",
        max_concurrent_turns: int = 32,
    ) -> None:
        self._router = router
        self._guard = guard
        self._generation = generation
        # ADR-0031: the turn runs as a background task in THIS process, so
        # this semaphore is the only thing standing between a chat burst
        # and the retrieval API's latency on the same event loop.
        self._turns = asyncio.Semaphore(max_concurrent_turns)

    async def prepare(self, *, subject: str, prompt: str, task: Task = Task.GENERATE) -> Turn:
        """Every way this call can be refused, decided up front.

        `generation=off` is ladder step 2a as a manual switch, and it
        raises the same `PlaneShed` as the spend breaker on purpose: one
        degraded mode to reason about, one branch for callers to write.
        """
        if self._generation == "off":
            raise PlaneShed("generation disarmed (ladder step 2a)")
        if not self._router.specs_for(task):
            raise NoProviderAvailable(f"no provider registered for task {task.value}")
        messages = [Message("system", DIAGNOSTIC_SYSTEM), Message("user", prompt)]
        fitted = await self._guard.admit(
            route=self._router.route(task), subject=subject, messages=messages
        )
        return Turn(task=task, subject=subject, messages=fitted)

    async def complete(self, turn: Turn) -> Completion:
        async with self._turns:
            completion = await self._router.complete(turn.task, turn.messages)
        await self._guard.settle(subject=turn.subject, tokens=completion.completion_tokens)
        return completion

    async def stream(self, turn: Turn) -> AsyncIterator[TokenChunk]:
        # The semaphore is held for the WHOLE stream, not just its start:
        # an in-flight generation occupies provider concurrency until its
        # last frame, so releasing early would let the cap be overrun by
        # exactly the number of streams still running.
        async with self._turns:
            completion_tokens = 0
            async for chunk in self._router.stream(turn.task, turn.messages):
                if chunk.done:
                    completion_tokens = chunk.completion_tokens
                yield chunk
        await self._guard.settle(subject=turn.subject, tokens=completion_tokens)
