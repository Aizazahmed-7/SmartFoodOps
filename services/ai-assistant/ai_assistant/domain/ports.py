"""Outbound ports for the GenAI plane (ADR-0030) — the ONLY seams through
which a model provider is reached. Adding Groq or Bedrock is a new adapter
file plus a router row; nothing else moves.

ADR-0010's discipline, transferred verbatim: a refusal, a length
truncation, a tool call and an empty answer are RESULTS, because they are
business outcomes the caller must branch on. Only transport failures are
exceptions. Getting this boundary wrong is how a content-policy refusal
ends up in a 500 and an alert.

`VectorStore` is deliberately NOT declared here yet. Its shape is driven by
chunking decisions that belong to B1 (ADR-0032/0033), and a port guessed a
milestone early is worse than a port declared on time.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]

FinishReason = Literal["stop", "length", "refusal", "tool_call"]
"""Why generation stopped.

stop      — the model finished its answer
length    — hit max_output_tokens; the answer is TRUNCATED, not wrong
refusal   — the provider declined on content grounds
tool_call — the model asked for a tool (B3; no tools are offered in B0)
"""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    finish_reason: FinishReason
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class TokenChunk:
    """One frame of a streamed answer.

    `text` is the delta to append. The terminal frame carries `done=True`,
    an empty delta, and the usage the provider only reveals at the end —
    which is why token accounting cannot be done from the deltas alone.
    """

    text: str = ""
    done: bool = False
    finish_reason: FinishReason | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LlmUnavailable(Exception):
    """Provider unreachable, timed out, or 5xx after bounded retries.

    Unlike `PspUnavailable`, the outcome is NOT ambiguous and nothing must
    be retained: a completion has no side effect at the provider, so a
    retry — or a failover to another vendor entirely — cannot double-charge
    anyone or leave books to reconcile (ADR-0030 §4).
    """


class LlmRateLimited(Exception):
    """Provider refused on quota (429 / tokens-per-minute exhausted).

    Distinct from `LlmUnavailable` because it is the EXPECTED failure at
    scale — quota, not availability, is the real ceiling (ADR-0029) — and
    because it should fail over to a different vendor immediately rather
    than burn the local retry budget against a wall.
    """


class LlmPort(Protocol):
    """One model provider. Model ids are passed in, never chosen here: the
    router owns task->model policy (ADR-0030 §3)."""

    @property
    def provider(self) -> str:
        """Stable short name used in metrics labels and router policy."""
        ...

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> Completion: ...

    def stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> AsyncIterator[TokenChunk]:
        """Not a coroutine — returns the iterator, so a caller can start it
        and observe whether the FIRST frame fails (the only point at which
        failover is still safe; see ModelRouter.stream)."""
        ...


class EmbeddingUnavailable(Exception):
    """Embedding provider unreachable after bounded retries. The knowledge
    consumer raises to retry (ADR-0021), so a provider outage delays the
    index and never drops a menu change."""


class EmbeddingPort(Protocol):
    """Text -> vectors. `model` and `dimensions` are exposed because both
    are written to every row: vectors from different models are not
    comparable, so a change to either is a version bump and a rolling
    reindex, never an in-place edit (PRD FR-61)."""

    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Batch by contract: per-text calls are the difference between one
        request and thirty at ingestion volume. Returns vectors positionally
        aligned with `texts`."""
        ...
