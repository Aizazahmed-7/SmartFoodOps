"""Task -> model policy, with cross-vendor failover (ADR-0030 §3-§4).

Callers name a TASK; they never name a model. That is the whole point: it
keeps "which model answers this" as one reviewable table instead of a
decision scattered across whoever wrote each graph node, and it makes the
cheap tier actually get used for cheap work — a ~20x cost and ~5x latency
difference, per turn, forever.

Instrumentation lives here rather than in the adapters because `task` is
the label that makes the numbers mean anything, and an adapter has no idea
what it is being used for.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter

from ..metrics import (
    PROVIDER_FAILOVERS,
    RESPONSE_SECONDS,
    TIME_TO_FIRST_TOKEN_SECONDS,
    TOKENS,
)
from .ports import Completion, LlmPort, LlmRateLimited, LlmUnavailable, Message, TokenChunk


class Task(StrEnum):
    """The closed task vocabulary. Closed on purpose: an open one is how a
    generation-tier model ends up classifying intents."""

    CLASSIFY = "classify"
    REWRITE = "rewrite"
    RERANK = "rerank"
    GENERATE = "generate"
    EXPLAIN = "explain"
    CONTENT_DRAFT = "content_draft"
    SUMMARIZE = "summarize"


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    max_output_tokens: int
    timeout_s: float


@dataclass(frozen=True)
class Route:
    """`max_input_tokens` sits on the route, not the spec, so both providers
    receive the IDENTICAL prompt. A failover that silently re-truncated
    would make the two answers incomparable and the failure unreproducible."""

    task: Task
    max_input_tokens: int
    primary: ModelSpec
    secondary: ModelSpec | None = None


class NoProviderAvailable(Exception):
    """Every provider for this task is unregistered (no key) or refused.
    Maps to 503 DEPENDENCY_UNAVAILABLE — never to a 500."""


def default_policy(
    *,
    generate_model: str,
    cheap_model: str,
    generate_fallback: str,
    cheap_fallback: str,
    timeout_s: float,
) -> dict[Task, Route]:
    """The policy table, built from Settings so model ids stay config.

    Output caps are per-task because they are a cost AND a quality control:
    a classifier that can emit 4k tokens is a bug waiting to bill.
    """

    def anthropic(model: str, out: int) -> ModelSpec:
        return ModelSpec("anthropic", model, max_output_tokens=out, timeout_s=timeout_s)

    def openai(model: str, out: int) -> ModelSpec:
        return ModelSpec("openai", model, max_output_tokens=out, timeout_s=timeout_s)

    return {
        # Structured extraction: turns "under $10, something light" into SQL
        # predicates. Short in, short out, and on the latency critical path.
        Task.CLASSIFY: Route(
            Task.CLASSIFY, 2_000, anthropic(cheap_model, 512), openai(cheap_fallback, 512)
        ),
        Task.REWRITE: Route(
            Task.REWRITE, 2_000, anthropic(cheap_model, 256), openai(cheap_fallback, 256)
        ),
        # Reranking sees up to 50 candidates, so the input cap is the large
        # one here while the output stays an ordered id list.
        Task.RERANK: Route(
            Task.RERANK, 12_000, anthropic(cheap_model, 512), openai(cheap_fallback, 512)
        ),
        Task.GENERATE: Route(
            Task.GENERATE, 8_000, anthropic(generate_model, 1_024), openai(generate_fallback, 1_024)
        ),
        # Explanations render a ReasonCode the resolver already decided
        # (ADR-0035), so they need almost no context and very little output.
        Task.EXPLAIN: Route(
            Task.EXPLAIN, 2_000, anthropic(cheap_model, 384), openai(cheap_fallback, 384)
        ),
        # Batch tasks: no user is waiting, so they get the good model and a
        # long leash, and they are the first thing a spend breaker starves.
        Task.CONTENT_DRAFT: Route(
            Task.CONTENT_DRAFT, 4_000, anthropic(generate_model, 1_024), None
        ),
        Task.SUMMARIZE: Route(Task.SUMMARIZE, 16_000, anthropic(generate_model, 1_024), None),
    }


class ModelRouter:
    def __init__(self, providers: Mapping[str, LlmPort], policy: Mapping[Task, Route]) -> None:
        for name, provider in providers.items():
            if provider.provider != name:
                # Registering {"anthropic": OpenAiLlm(...)} would otherwise
                # send every anthropic-routed task to OpenAI and label the
                # metrics with the wrong vendor — a wiring bug that shows up
                # only as a confusing cost report. Fail at construction.
                raise ValueError(
                    f"provider registered as {name!r} identifies as {provider.provider!r}"
                )
        self._providers = dict(providers)
        self._policy = dict(policy)

    def route(self, task: Task) -> Route:
        return self._policy[task]

    def specs_for(self, task: Task) -> list[ModelSpec]:
        """Only specs whose provider is actually registered — an empty key
        removes a provider from the fleet rather than producing 401s."""
        route = self.route(task)
        candidates = [route.primary] + ([route.secondary] if route.secondary else [])
        return [spec for spec in candidates if spec.provider in self._providers]

    def _count(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        if prompt_tokens:
            TOKENS.labels(model=model, direction="prompt").inc(prompt_tokens)
        if completion_tokens:
            TOKENS.labels(model=model, direction="completion").inc(completion_tokens)

    def _failover(self, task: Task, previous: ModelSpec, nxt: ModelSpec, exc: Exception) -> None:
        PROVIDER_FAILOVERS.labels(
            task=task.value,
            from_provider=previous.provider,
            to_provider=nxt.provider,
            reason="rate_limited" if isinstance(exc, LlmRateLimited) else "unavailable",
        ).inc()

    async def complete(self, task: Task, messages: Sequence[Message]) -> Completion:
        specs = self.specs_for(task)
        if not specs:
            raise NoProviderAvailable(f"no provider registered for task {task.value}")
        last: Exception | None = None
        for index, spec in enumerate(specs):
            started = perf_counter()
            try:
                completion = await self._providers[spec.provider].complete(
                    model=spec.model,
                    messages=messages,
                    max_output_tokens=spec.max_output_tokens,
                    timeout_s=spec.timeout_s,
                )
            except (LlmUnavailable, LlmRateLimited) as exc:
                RESPONSE_SECONDS.labels(
                    task=task.value, model=spec.model, outcome="unavailable"
                ).observe(perf_counter() - started)
                last = exc
                if index + 1 < len(specs):
                    self._failover(task, spec, specs[index + 1], exc)
                continue
            RESPONSE_SECONDS.labels(
                task=task.value, model=spec.model, outcome=completion.finish_reason
            ).observe(perf_counter() - started)
            self._count(spec.model, completion.prompt_tokens, completion.completion_tokens)
            return completion
        raise NoProviderAvailable(f"every provider failed for task {task.value}") from last

    async def stream(self, task: Task, messages: Sequence[Message]) -> AsyncIterator[TokenChunk]:
        """Stream with failover that stops at the first delivered token.

        This is the one rule that matters: once a frame has reached the
        reader, switching providers would splice two different answers
        together mid-sentence. So a failure BEFORE the first frame fails
        over, and a failure AFTER it propagates — the stream ends, the
        client sees an error frame, and the user re-asks. A half-answer
        from one model finished by another is worse than a visible failure.
        """
        specs = self.specs_for(task)
        if not specs:
            raise NoProviderAvailable(f"no provider registered for task {task.value}")
        last: Exception | None = None
        for index, spec in enumerate(specs):
            started = perf_counter()
            emitted = False
            try:
                iterator = self._providers[spec.provider].stream(
                    model=spec.model,
                    messages=messages,
                    max_output_tokens=spec.max_output_tokens,
                    timeout_s=spec.timeout_s,
                )
                async for chunk in iterator:
                    if not emitted and chunk.text:
                        TIME_TO_FIRST_TOKEN_SECONDS.labels(
                            task=task.value, model=spec.model
                        ).observe(perf_counter() - started)
                        emitted = True
                    if chunk.done:
                        self._count(spec.model, chunk.prompt_tokens, chunk.completion_tokens)
                        RESPONSE_SECONDS.labels(
                            task=task.value,
                            model=spec.model,
                            outcome=chunk.finish_reason or "stop",
                        ).observe(perf_counter() - started)
                    yield chunk
                return
            except (LlmUnavailable, LlmRateLimited) as exc:
                RESPONSE_SECONDS.labels(
                    task=task.value, model=spec.model, outcome="unavailable"
                ).observe(perf_counter() - started)
                if emitted:
                    raise
                last = exc
                if index + 1 < len(specs):
                    self._failover(task, spec, specs[index + 1], exc)
        raise NoProviderAvailable(f"every provider failed for task {task.value}") from last
