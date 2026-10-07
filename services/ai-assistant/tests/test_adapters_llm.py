"""The two provider adapters, over httpx.MockTransport.

These tests ARE the vendor contract. Every wire fact the adapters depend
on — Anthropic's top-level `system`, its usage split across message_start
and message_delta, OpenAI's `max_completion_tokens` and its
`stream_options.include_usage` requirement — is asserted here, so a vendor
change surfaces as a named failure instead of a runtime mystery.
"""

import json

import httpx
import pytest
from ai_assistant.adapters._http import RetryPolicy
from ai_assistant.adapters.llm_anthropic import AnthropicLlm
from ai_assistant.adapters.llm_openai import OpenAiLlm
from ai_assistant.domain.ports import LlmRateLimited, LlmUnavailable, Message, TokenChunk
from ai_assistant.domain.router import Task

FAST = RetryPolicy(attempts=3, delay_s=0.0)
ASK = [Message("system", "be brief"), Message("user", "something light")]


def sse(*frames: dict) -> bytes:
    return "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames).encode()


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def anthropic(handler) -> AnthropicLlm:
    return AnthropicLlm(api_key="k", base_url="https://api.test/", http=client(handler), retry=FAST)


def openai(handler) -> OpenAiLlm:
    return OpenAiLlm(api_key="k", base_url="https://api.test/", http=client(handler), retry=FAST)


async def drain(iterator) -> list[TokenChunk]:
    return [chunk async for chunk in iterator]


# --- Anthropic ------------------------------------------------------------


async def test_anthropic_lifts_system_out_of_the_message_list():
    """Anthropic takes the system prompt as a top-level field, not a role.
    Every other layer here treats it as message zero, so the split happens
    once, at the boundary."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.headers["x-api-key"] == "k"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "model": "sonnet",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "a broth"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    result = await anthropic(handler).complete(
        model="sonnet", messages=ASK, max_output_tokens=64, timeout_s=1.0
    )
    assert seen["system"] == "be brief"
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert seen["max_tokens"] == 64
    assert seen["temperature"] == 0
    assert result.text == "a broth"
    assert result.provider == "anthropic"
    assert (result.prompt_tokens, result.completion_tokens) == (5, 2)


async def test_anthropic_omits_system_when_there_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "system" not in json.loads(request.content)
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"}
        )

    await anthropic(handler).complete(
        model="sonnet",
        messages=[Message("user", "hi")],
        max_output_tokens=8,
        timeout_s=1.0,
    )


async def test_anthropic_keeps_only_text_blocks():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stop_reason": "max_tokens",
                "content": [
                    {"type": "text", "text": "kept "},
                    {"type": "thinking", "text": "dropped"},
                    "not-a-dict",
                    {"type": "text", "text": "also kept"},
                ],
            },
        )

    result = await anthropic(handler).complete(
        model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
    )
    assert result.text == "kept also kept"
    assert result.finish_reason == "length"


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_call"),
        ("refusal", "refusal"),
        ("something_new_from_the_vendor", "stop"),
    ],
)
async def test_anthropic_maps_every_stop_reason(stop_reason: str, expected: str):
    """An unknown reason degrades to "stop": a new vendor value must not
    turn a complete answer into an exception."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stop_reason": stop_reason, "content": []})

    result = await anthropic(handler).complete(
        model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
    )
    assert result.finish_reason == expected


async def test_anthropic_streams_deltas_and_usage_from_two_frames():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a "}},
        {"type": "content_block_delta", "delta": {"type": "signature", "text": "ignored"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "broth"}},
        {"type": "ping"},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=body)

    chunks = await drain(
        anthropic(handler).stream(model="sonnet", messages=ASK, max_output_tokens=64, timeout_s=1.0)
    )
    assert "".join(chunk.text for chunk in chunks) == "a broth"
    terminal = chunks[-1]
    assert terminal.done and terminal.prompt_tokens == 9 and terminal.completion_tokens == 3


async def test_anthropic_raises_on_a_mid_stream_error_frame():
    body = sse(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a li"}},
        {"type": "error", "error": {"message": "overloaded"}},
    )
    seen: list[str] = []
    with pytest.raises(LlmUnavailable, match="overloaded"):
        async for chunk in anthropic(lambda _: httpx.Response(200, content=body)).stream(
            model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
        ):
            seen.append(chunk.text)
    assert seen == ["a li"]


# --- Failure taxonomy (shared plumbing, asserted at the port) -------------


async def test_rate_limit_is_its_own_exception_so_the_router_can_fail_over():
    with pytest.raises(LlmRateLimited):
        await anthropic(lambda _: httpx.Response(429, text="slow down")).complete(
            model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
        )


async def test_a_4xx_that_is_not_429_is_not_retried():
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad model id")

    with pytest.raises(LlmUnavailable, match="refused"):
        await anthropic(handler).complete(
            model="nope", messages=ASK, max_output_tokens=8, timeout_s=1.0
        )
    assert len(calls) == 1  # retrying our own bug just makes it three times


async def test_a_5xx_is_retried_then_succeeds():
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="upstream")
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": []})

    await anthropic(handler).complete(
        model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
    )
    assert len(calls) == 3


async def test_exhausted_retries_surface_as_unavailable():
    with pytest.raises(LlmUnavailable, match="provider 500"):
        await anthropic(lambda _: httpx.Response(500, text="boom")).complete(
            model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
        )


async def test_a_transport_error_is_retried_and_then_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(LlmUnavailable, match="unreachable"):
        await anthropic(handler).complete(
            model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
        )


async def test_stream_handshake_failures_map_at_the_port():
    with pytest.raises(LlmRateLimited):
        await drain(
            anthropic(lambda _: httpx.Response(429, text="tpm")).stream(
                model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
            )
        )
    with pytest.raises(LlmUnavailable, match="refused"):
        await drain(
            anthropic(lambda _: httpx.Response(404, text="gone")).stream(
                model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
            )
        )


async def test_stream_handshake_is_retried_on_5xx():
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(502, text="gateway")
        return httpx.Response(200, content=sse({"type": "message_stop"}))

    await drain(
        anthropic(handler).stream(model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0)
    )
    assert len(calls) == 2


async def test_stream_handshake_retries_a_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(LlmUnavailable, match="unreachable"):
        await drain(
            anthropic(handler).stream(
                model="sonnet", messages=ASK, max_output_tokens=8, timeout_s=1.0
            )
        )


# --- OpenAI ---------------------------------------------------------------


async def test_openai_sends_max_completion_tokens_and_system_as_a_role():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(
            200,
            json={
                "model": "gpt",
                "choices": [{"message": {"content": "a broth"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    result = await openai(handler).complete(
        model="gpt", messages=ASK, max_output_tokens=32, timeout_s=1.0
    )
    assert seen["max_completion_tokens"] == 32
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert result.provider == "openai"
    assert (result.prompt_tokens, result.completion_tokens) == (4, 2)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_call"),
        ("content_filter", "refusal"),
        ("brand_new", "stop"),
    ],
)
async def test_openai_maps_every_finish_reason(reason: str, expected: str):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {}, "finish_reason": reason}]})

    result = await openai(handler).complete(
        model="gpt", messages=ASK, max_output_tokens=8, timeout_s=1.0
    )
    assert result.finish_reason == expected


async def test_openai_tolerates_a_response_with_no_choices():
    result = await openai(lambda _: httpx.Response(200, json={})).complete(
        model="gpt", messages=ASK, max_output_tokens=8, timeout_s=1.0
    )
    assert result.text == ""


async def test_openai_requests_usage_on_streams_and_reads_the_trailing_frame():
    """Without stream_options.include_usage the streamed response carries
    NO usage and token accounting silently reads zero."""
    body = (
        sse(
            {"choices": [{"delta": {"content": "a "}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "broth"}, "finish_reason": None}]},
            {"choices": ["not-a-dict"]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
        )
        + b"data: [DONE]\n\n"
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=body)

    chunks = await drain(
        openai(handler).stream(model="gpt", messages=ASK, max_output_tokens=32, timeout_s=1.0)
    )
    assert seen["stream_options"] == {"include_usage": True}
    assert "".join(chunk.text for chunk in chunks) == "a broth"
    terminal = chunks[-1]
    assert terminal.done and (terminal.prompt_tokens, terminal.completion_tokens) == (4, 2)


async def test_openai_stream_handshake_failure_maps_at_the_port():
    with pytest.raises(LlmUnavailable):
        await drain(
            openai(lambda _: httpx.Response(500, text="boom")).stream(
                model="gpt", messages=ASK, max_output_tokens=8, timeout_s=1.0
            )
        )


async def test_openai_complete_failure_maps_at_the_port():
    with pytest.raises(LlmRateLimited):
        await openai(lambda _: httpx.Response(429, text="tpm")).complete(
            model="gpt", messages=ASK, max_output_tokens=8, timeout_s=1.0
        )


def test_the_shipped_adapters_identify_as_their_policy_keys():
    """The router refuses a provider registered under the wrong name, so
    the adapters' PROVIDER constants and default_policy's keys must agree.
    This is what catches a rename that would otherwise route every
    anthropic task to OpenAI and mislabel the cost report."""
    from ai_assistant.domain.router import ModelRouter, default_policy

    def noop(_: httpx.Request) -> httpx.Response:  # pragma: no cover — never sent
        raise AssertionError("construction must not call the provider")

    policy = default_policy(
        generate_model="sonnet",
        cheap_model="haiku",
        generate_fallback="gpt",
        cheap_fallback="gpt-mini",
        timeout_s=1.0,
    )
    router = ModelRouter({"anthropic": anthropic(noop), "openai": openai(noop)}, policy)
    assert [spec.provider for spec in router.specs_for(Task.GENERATE)] == [
        "anthropic",
        "openai",
    ]
