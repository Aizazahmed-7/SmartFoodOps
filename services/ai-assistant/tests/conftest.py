"""Fakes and fixtures for the AI plane.

`FakeLlm` is `FakeGateway`'s shape (services/payment/tests/conftest.py):
a `script` list that pops either a result or an Exception instance to
raise, plus a record of every call. That is the whole answer to testing a
non-deterministic dependency under a 100% coverage gate — refusal,
truncation, rate-limit, unavailability and mid-stream failure each become
one list entry, deterministically, with no network and no API key.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from ai_assistant.config import Settings
from ai_assistant.domain.ports import Completion, Message, TokenChunk
from ai_assistant.main import create_app
from fastapi.testclient import TestClient


def completion(text: str = "a light soup", **kwargs: Any) -> Completion:
    defaults: dict[str, Any] = {
        "text": text,
        "finish_reason": "stop",
        "model": "fake-model",
        "provider": "anthropic",
        "prompt_tokens": 11,
        "completion_tokens": 7,
    }
    defaults.update(kwargs)
    return Completion(**defaults)


def frames(*texts: str, **kwargs: Any) -> list[TokenChunk]:
    """A token script ending in the terminal usage frame the providers only
    reveal at the end."""
    terminal: dict[str, Any] = {
        "done": True,
        "finish_reason": "stop",
        "prompt_tokens": 11,
        "completion_tokens": len(texts),
    }
    terminal.update(kwargs)
    return [TokenChunk(text=text) for text in texts] + [TokenChunk(**terminal)]


class FakeLlm:
    """Scripted LlmPort. Empty scripts fall back to a benign default so a
    test that only cares about one behaviour need not set up the rest."""

    def __init__(self, provider: str = "anthropic") -> None:
        self._provider = provider
        self.script: list[Completion | Exception] = []
        # Sequence, not list: `frames(...)` is a list[TokenChunk] and list
        # is invariant, so a list[...] annotation would reject it.
        self.stream_script: list[Sequence[TokenChunk | Exception]] = []
        self.calls: list[tuple[str, str, list[Message]]] = []

    @property
    def provider(self) -> str:
        return self._provider

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> Completion:
        self.calls.append(("complete", model, list(messages)))
        step = self.script.pop(0) if self.script else completion(provider=self._provider)
        if isinstance(step, Exception):
            raise step
        return step

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> AsyncIterator[TokenChunk]:
        self.calls.append(("stream", model, list(messages)))
        script = self.stream_script.pop(0) if self.stream_script else frames("ok")
        for frame in script:
            if isinstance(frame, Exception):
                raise frame
            yield frame


class FakeBudgetStore:
    """Windowed counters in a dict. `hard_cap` forces a refusal on a named
    key so breaker-open and user-exhausted are one line apart in a test."""

    def __init__(self) -> None:
        self.totals: dict[str, int] = {}
        self.windows: dict[str, int] = {}
        self.refuse: set[str] = set()

    async def consume(self, key: str, tokens: int, *, budget: int, window_s: int) -> bool:
        self.totals[key] = self.totals.get(key, 0) + tokens
        self.windows[key] = window_s
        if key in self.refuse:
            return False
        return self.totals[key] <= budget


def settings(**kwargs: Any) -> Settings:
    base: dict[str, Any] = {"database_url": "sqlite+aiosqlite://", "create_all": True}
    base.update(kwargs)
    return Settings(**base)


@pytest.fixture()
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture()
def store() -> FakeBudgetStore:
    return FakeBudgetStore()


@pytest.fixture()
def client(llm: FakeLlm, store: FakeBudgetStore):
    """The REAL router, guard and service over a fake provider — the
    failover rule and the budget arithmetic are the parts worth testing,
    and a coarse `service=` override would skip both."""
    app = create_app(settings(), providers={"anthropic": llm}, budget_store=store)
    with TestClient(app) as test_client:  # `with` runs the lifespan (create_all)
        yield test_client
