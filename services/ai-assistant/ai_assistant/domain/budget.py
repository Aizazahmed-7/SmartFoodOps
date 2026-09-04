"""Three nested spend limits (ADR-0030 §5), all fail-closed.

Every other resource in Part A fails closed when over-consumed: Redis
evicts, the edge 429s, DynamoDB throttles. A metered third-party API does
not — it bills. So the guard exists to make the AI plane behave like the
rest of the fleet: refuse before spending, never after.

The three limits answer different questions and none substitutes for
another. The per-request cap bounds ONE prompt (the runaway-context
failure). The per-user counter bounds one abuser (request-rate limiting at
the edge cannot see tokens — a single request can be 100x another). The
per-cell counter bounds the invoice as a whole.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from typing import Protocol

from ..metrics import BUDGET_REFUSALS, CONTEXT_TRUNCATIONS
from .ports import Message
from .router import Route


class BudgetStore(Protocol):
    """Shared, windowed spend counters. Redis in deployment, absent in dev.

    A fixed-window counter, not a true token bucket — deliberately, and
    named honestly: the failure mode is tolerating up to 2x budget across a
    window boundary, which for a spend ceiling is noise. Returns False when
    the increment would exceed `budget`.
    """

    async def consume(self, key: str, tokens: int, *, budget: int, window_s: int) -> bool: ...


class BudgetExceeded(Exception):
    """This subject's token budget is exhausted. Maps to 429 RATE_LIMITED."""


class PlaneShed(Exception):
    """The per-cell spend/quota breaker is open. Maps to 503 ADMISSION_SHED,
    not DEPENDENCY_UNAVAILABLE: nothing is broken, we chose to stop
    spending. Callers with a non-generative answer available (lexical
    search, popularity recommendations, a rendered template) must catch this
    and serve that instead — ladder step 2a. A breaker that errors where it
    could return a worse-but-real answer is a worse breaker.
    """


def estimate_tokens(text: str) -> int:
    """Deliberately approximate: ~4 characters per token.

    Real tokenizers differ per vendor family, and ADR-0030 already records
    that this will be approximate for at least one of them. It is injectable
    so a real tokenizer can replace it without touching the guard. What
    matters is that it is counted BEFORE the call — a prompt is never sent
    to find out how big it was.
    """
    return max(1, (len(text) + 3) // 4)


class BudgetGuard:
    def __init__(
        self,
        *,
        cell_id: str,
        user_budget: int,
        user_window_s: int,
        cell_budget: int,
        cell_window_s: int,
        store: BudgetStore | None = None,
        estimate: Callable[[str], int] = estimate_tokens,
    ) -> None:
        self._cell_id = cell_id
        self._user_budget = user_budget
        self._user_window_s = user_window_s
        self._cell_budget = cell_budget
        self._cell_window_s = cell_window_s
        self._store = store
        self._estimate = estimate

    def _cost(self, messages: Iterable[Message]) -> int:
        return sum(self._estimate(message.content) for message in messages)

    def _clip(self, content: str, budget_tokens: int) -> str:
        # Only ever called with budget_tokens >= 1: fit() raises before it
        # gets here if the system prompt alone filled the cap.
        # cost > budget_tokens is guaranteed by the caller: _clip only runs
        # for a message that already failed to fit.
        cost = self._estimate(content)
        clipped = content[: max(1, int(len(content) * budget_tokens / cost))]
        while clipped and self._estimate(clipped) > budget_tokens:
            clipped = clipped[: int(len(clipped) * 0.9)]
        return clipped

    def fit(self, route: Route, messages: Sequence[Message]) -> tuple[list[Message], int]:
        """Trim a conversation to the route's input cap. Three rules:

        1. **Every system message survives.** They carry the grounding and
           safety instructions; dropping one to save tokens is a safety
           failure dressed as an optimisation, so it never happens.
        2. **History is kept newest-first and CONTIGUOUS.** Walking back
           from the newest turn and stopping at the first message that does
           not fit is what keeps the dialogue coherent — skipping a middle
           turn to squeeze in an older one produces context that reads as a
           non-sequitur to the model.
        3. **The newest turn is never dropped**, only clipped. Answering
           the history and ignoring the actual question would be absurd.
        """
        system = [m for m in messages if m.role == "system"]
        history = [m for m in messages if m.role != "system"]
        remaining = route.max_input_tokens - self._cost(system)
        if remaining <= 0:
            raise ValueError(
                f"system prompt alone exceeds the {route.task.value} input cap "
                f"({route.max_input_tokens}) — a policy/config bug, not a runtime condition"
            )

        kept: list[Message] = []
        truncated = False
        for message in reversed(history):
            cost = self._estimate(message.content)
            if cost <= remaining:
                kept.append(message)
                remaining -= cost
                continue
            if not kept:
                clipped = replace(message, content=self._clip(message.content, remaining))
                kept.append(clipped)
                remaining -= self._estimate(clipped.content)
                truncated = True
            break

        kept.reverse()
        if truncated or len(kept) < len(history):
            CONTEXT_TRUNCATIONS.labels(task=route.task.value).inc()
        fitted = system + kept
        return fitted, self._cost(fitted)

    async def admit(
        self, *, route: Route, subject: str, messages: Sequence[Message]
    ) -> list[Message]:
        """Fit the prompt, then charge the input half of the call.

        The cell breaker is checked first: if the plane is shedding, nobody
        is served, so there is no reason to spend a user's budget on a
        request that will be refused anyway. The reverse order also leaks
        cell budget on every user-refused request.

        Cell tokens are consumed before the user check, so a user refusal
        leaves them spent. That is deliberate — over-counting sheds slightly
        EARLIER, and for a spend ceiling erring early is the safe direction.
        """
        fitted, tokens = self.fit(route, messages)
        if self._store is None:
            return fitted
        if not await self._store.consume(
            f"assistant:cb:{self._cell_id}",
            tokens,
            budget=self._cell_budget,
            window_s=self._cell_window_s,
        ):
            BUDGET_REFUSALS.labels(reason="breaker_open").inc()
            raise PlaneShed(f"cell {self._cell_id} token budget exhausted")
        if not await self._store.consume(
            f"assistant:budget:{subject}",
            tokens,
            budget=self._user_budget,
            window_s=self._user_window_s,
        ):
            BUDGET_REFUSALS.labels(reason="user_budget").inc()
            raise BudgetExceeded("token budget exhausted for this subject")
        return fitted

    async def settle(self, *, subject: str, tokens: int) -> None:
        """Charge the output half, once the provider reveals it.

        Never raises and never refuses: the tokens are already spent and
        billed, so the only useful action is to record them so the NEXT
        call sees a truthful counter. Splitting admit/settle this way means
        each half is charged exactly when it becomes knowable — no
        estimate-then-refund arithmetic to get wrong.
        """
        if self._store is None or tokens <= 0:
            return
        await self._store.consume(
            f"assistant:cb:{self._cell_id}",
            tokens,
            budget=self._cell_budget,
            window_s=self._cell_window_s,
        )
        await self._store.consume(
            f"assistant:budget:{subject}",
            tokens,
            budget=self._user_budget,
            window_s=self._user_window_s,
        )
