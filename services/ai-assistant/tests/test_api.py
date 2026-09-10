"""The internal diagnostic surface, and the SSE framing it proves.

The framing assertion that matters: model output contains newlines, and a
newline inside an SSE `data:` line silently ends the field. Token payloads
are therefore JSON-encoded, and `test_multiline_output_survives_the_wire`
is what keeps that true.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator

from ai_assistant.api.routes import _frames
from ai_assistant.domain.ports import LlmUnavailable, Message, TokenChunk
from ai_assistant.domain.router import Task
from ai_assistant.domain.service import Turn
from ai_assistant.main import create_app
from fastapi.testclient import TestClient

from .conftest import FakeBudgetStore, FakeLlm, frames, settings

SYSTEM = {"X-Auth-Sub": "svc:test", "X-Auth-Roles": "system"}
ECHO = "/v1/internal/assistant/echo"
STREAM = "/v1/internal/assistant/echo/stream"


def events(raw: str) -> list[tuple[str, object]]:
    """Parse an SSE body into (event, decoded data) pairs."""
    out: list[tuple[str, object]] = []
    name = ""
    for line in raw.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            out.append((name, json.loads(line[6:])))
    return out


def test_echo_returns_the_completion_and_its_token_accounting(client: TestClient):
    response = client.post(ECHO, json={"prompt": "something light"}, headers=SYSTEM)
    assert response.status_code == 200
    assert response.json() == {
        "text": "a light soup",
        "model": "fake-model",
        "provider": "anthropic",
        "finish_reason": "stop",
        "prompt_tokens": 11,
        "completion_tokens": 7,
    }


def test_echo_honours_the_task_so_cheap_work_reaches_the_cheap_tier(
    client: TestClient, llm: FakeLlm
):
    client.post(ECHO, json={"prompt": "classify me", "task": "classify"}, headers=SYSTEM)
    assert llm.calls[0][1] == "claude-haiku-4-5-20251001"


def test_echo_is_system_only(client: TestClient):
    assert client.post(ECHO, json={"prompt": "x"}).status_code == 401
    assert (
        client.post(
            ECHO, json={"prompt": "x"}, headers={"X-Auth-Sub": "usr_1", "X-Auth-Roles": "customer"}
        ).status_code
        == 403
    )


def test_echo_rejects_unknown_fields_and_empty_prompts(client: TestClient):
    assert client.post(ECHO, json={"prompt": ""}, headers=SYSTEM).status_code == 422
    assert (
        client.post(ECHO, json={"prompt": "x", "model": "sneaky"}, headers=SYSTEM).status_code
        == 422
    )


def test_echo_reports_a_shed_plane_as_admission_shed():
    app = create_app(settings(generation="off"), providers={"anthropic": FakeLlm()})
    with TestClient(app) as client:
        response = client.post(ECHO, json={"prompt": "x"}, headers=SYSTEM)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ADMISSION_SHED"
    assert response.headers["Retry-After"] == "30"


def test_echo_reports_an_exhausted_budget_as_rate_limited():
    store = FakeBudgetStore()
    store.refuse.add("assistant:budget:svc:test")
    app = create_app(settings(), providers={"anthropic": FakeLlm()}, budget_store=store)
    with TestClient(app) as client:
        response = client.post(ECHO, json={"prompt": "x"}, headers=SYSTEM)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_echo_reports_no_configured_provider_as_dependency_unavailable():
    """Zero registered providers is a legal, deliberate state: an empty key
    removes a provider from the fleet rather than producing 401s."""
    app = create_app(settings(), providers={})
    with TestClient(app) as client:
        response = client.post(ECHO, json={"prompt": "x"}, headers=SYSTEM)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_echo_reports_a_failing_provider_as_dependency_unavailable(
    client: TestClient, llm: FakeLlm
):
    llm.script = [LlmUnavailable("down")]
    response = client.post(ECHO, json={"prompt": "x"}, headers=SYSTEM)
    assert response.status_code == 503


def test_stream_delivers_tokens_then_a_done_frame(client: TestClient, llm: FakeLlm):
    llm.stream_script = [frames("a ", "light ", "soup")]
    with client.stream(
        "POST", STREAM, json={"prompt": "something light"}, headers=SYSTEM
    ) as response:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        body = "".join(response.iter_text())
    parsed = events(body)
    assert [name for name, _ in parsed] == ["token", "token", "token", "done"]
    assert "".join(str(data) for name, data in parsed if name == "token") == "a light soup"
    assert parsed[-1][1] == {"finish_reason": "stop", "prompt_tokens": 11, "completion_tokens": 3}


def test_multiline_output_survives_the_wire(client: TestClient, llm: FakeLlm):
    """A raw newline inside `data:` ends the field. JSON-encoding the token
    payload is the only reason a multi-line answer arrives intact."""
    llm.stream_script = [frames("line one\nline two\n\nline four")]
    with client.stream("POST", STREAM, json={"prompt": "x"}, headers=SYSTEM) as response:
        body = "".join(response.iter_text())
    tokens = [data for name, data in events(body) if name == "token"]
    assert tokens == ["line one\nline two\n\nline four"]


def test_stream_refuses_before_the_response_starts(client: TestClient):
    app = create_app(settings(generation="off"), providers={"anthropic": FakeLlm()})
    with TestClient(app) as shed_client:
        response = shed_client.post(STREAM, json={"prompt": "x"}, headers=SYSTEM)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ADMISSION_SHED"


def test_stream_refuses_an_exhausted_budget_with_a_status_code_not_a_frame():
    store = FakeBudgetStore()
    store.refuse.add("assistant:budget:svc:test")
    app = create_app(settings(), providers={"anthropic": FakeLlm()}, budget_store=store)
    with TestClient(app) as client:
        response = client.post(STREAM, json={"prompt": "x"}, headers=SYSTEM)
    assert response.status_code == 429


def test_stream_refuses_when_no_provider_is_registered():
    app = create_app(settings(), providers={})
    with TestClient(app) as client:
        assert client.post(STREAM, json={"prompt": "x"}, headers=SYSTEM).status_code == 503


def test_stream_reports_a_mid_answer_provider_failure_as_an_error_frame(
    client: TestClient, llm: FakeLlm
):
    """Once frames are delivered the router will not fail over, so the only
    honest report is inside the stream the reader is already holding."""
    llm.stream_script = [[TokenChunk(text="a li"), LlmUnavailable("reset")]]
    with client.stream("POST", STREAM, json={"prompt": "x"}, headers=SYSTEM) as response:
        body = "".join(response.iter_text())
    assert events(body) == [("token", "a li"), ("error", "provider_unavailable")]


# --- Paths HTTP cannot reach: drive _frames directly ----------------------


class ScriptedService:
    """A stand-in for AssistantService that can hang or be interrupted —
    neither is expressible through a scripted provider."""

    def __init__(self, *, hang: bool = False) -> None:
        self._hang = hang

    async def stream(self, turn: Turn) -> AsyncIterator[TokenChunk]:
        yield TokenChunk(text="partial")
        if self._hang:
            await asyncio.sleep(60)
        yield TokenChunk(done=True, finish_reason="stop")


def turn() -> Turn:
    return Turn(task=Task.GENERATE, subject="svc:test", messages=[Message("user", "x")])


async def test_a_wedged_provider_is_reaped_by_the_safety_lifetime():
    """120s in production: the lifetime exists to reap a wedged provider,
    not to rebalance a fleet the way the tracking lane's 15-30min does."""
    collected = [
        frame
        async for frame in _frames(ScriptedService(hang=True), turn(), lifetime_s=0.01)  # type: ignore[arg-type]
    ]
    assert events("".join(collected)) == [("token", "partial"), ("error", "lifetime")]


async def test_a_client_disconnect_closes_the_generator_and_is_counted():
    # AsyncGenerator, not just AsyncIterator: aclose() is the whole point.
    generator: AsyncGenerator[str, None] = _frames(
        ScriptedService(hang=True),  # type: ignore[arg-type]
        turn(),
        lifetime_s=5.0,
    )
    assert "partial" in await generator.__anext__()
    # aclose() throws GeneratorExit at the suspended yield — exactly what
    # Starlette does when the reader goes away mid-answer.
    await generator.aclose()


async def test_a_complete_stream_records_its_terminal_frame():
    collected = [
        frame
        async for frame in _frames(ScriptedService(), turn(), lifetime_s=5.0)  # type: ignore[arg-type]
    ]
    assert [name for name, _ in events("".join(collected))] == ["token", "done"]
