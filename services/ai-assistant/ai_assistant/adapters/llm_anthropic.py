"""Anthropic Messages API behind LlmPort (ADR-0030).

The two vendor-contract facts this file encodes, both pinned by
MockTransport tests so a vendor change surfaces as a named test failure
rather than a runtime mystery:

1. **System prompts are a top-level `system` field, not a message role.**
   Every other part of this codebase treats the system prompt as message
   zero, so the split happens here, at the boundary, once.
2. **Usage arrives in two different frames.** `message_start` carries
   input tokens, `message_delta` carries output tokens and the stop reason.
   Token accounting therefore cannot be derived from the text deltas — the
   terminal TokenChunk is the only frame that knows what the call cost.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any, NoReturn

import httpx

from ..domain.ports import (
    Completion,
    FinishReason,
    LlmRateLimited,
    LlmUnavailable,
    Message,
    TokenChunk,
)
from ._http import HttpCallFailed, RetryPolicy, open_stream, post_json, sse_payloads

PROVIDER = "anthropic"

_STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_call",
    "refusal": "refusal",
}


def _finish(stop_reason: object) -> FinishReason:
    """Unknown stop reasons degrade to "stop": a new vendor reason must not
    turn a complete answer into an exception."""
    return _STOP_REASONS.get(str(stop_reason), "stop")


def _raise(exc: HttpCallFailed) -> NoReturn:
    """NoReturn, not None: the callers use `except ... as exc: _raise(exc)`
    and then keep using the response variable. Without NoReturn pyright
    correctly flags it as possibly unbound."""
    if exc.rate_limited:
        raise LlmRateLimited(str(exc)) from exc
    raise LlmUnavailable(str(exc)) from exc


def _split(messages: Sequence[Message]) -> tuple[str, list[dict[str, str]]]:
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    turns = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return system, turns


class AnthropicLlm:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        api_version: str = "2023-06-01",
        retry: RetryPolicy | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = http
        self._version = api_version
        self._retry = retry or RetryPolicy()

    @property
    def provider(self) -> str:
        return PROVIDER

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }

    def _body(
        self, model: str, messages: Sequence[Message], max_output_tokens: int, stream: bool
    ) -> dict[str, Any]:
        system, turns = _split(messages)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "messages": turns,
            # Temperature 0 is not determinism (batching and float
            # non-associativity still move outputs), but it is the least
            # variance available and the right default for grounded answers.
            "temperature": 0,
        }
        if system:
            body["system"] = system
        if stream:
            body["stream"] = True
        return body

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> Completion:
        try:
            payload = await post_json(
                self._http,
                f"{self._base}/v1/messages",
                headers=self._headers(),
                body=self._body(model, messages, max_output_tokens, stream=False),
                timeout_s=timeout_s,
                retry=self._retry,
            )
        except HttpCallFailed as exc:
            _raise(exc)
        blocks = payload.get("content") or []
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = payload.get("usage") or {}
        return Completion(
            text=text,
            finish_reason=_finish(payload.get("stop_reason")),
            model=str(payload.get("model", model)),
            provider=PROVIDER,
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
        )

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        max_output_tokens: int,
        timeout_s: float,
    ) -> AsyncIterator[TokenChunk]:
        try:
            response = await open_stream(
                self._http,
                f"{self._base}/v1/messages",
                headers=self._headers(),
                body=self._body(model, messages, max_output_tokens, stream=True),
                timeout_s=timeout_s,
                retry=self._retry,
            )
        except HttpCallFailed as exc:
            _raise(exc)
        prompt_tokens = 0
        completion_tokens = 0
        finish: FinishReason = "stop"
        try:
            async for frame in sse_payloads(response):
                kind = frame.get("type")
                if kind == "message_start":
                    usage = (frame.get("message") or {}).get("usage") or {}
                    prompt_tokens = int(usage.get("input_tokens", 0))
                elif kind == "content_block_delta":
                    delta = frame.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield TokenChunk(text=str(delta.get("text", "")))
                elif kind == "message_delta":
                    finish = _finish((frame.get("delta") or {}).get("stop_reason"))
                    completion_tokens = int((frame.get("usage") or {}).get("output_tokens", 0))
                elif kind == "error":
                    # A mid-stream provider error. The router will NOT fail
                    # over once frames have been delivered — the stream ends
                    # and the reader sees a failure, which beats splicing
                    # two models' prose together.
                    detail = str((frame.get("error") or {}).get("message", "stream error"))
                    raise LlmUnavailable(f"anthropic stream error: {detail}")
        finally:
            await response.aclose()
        yield TokenChunk(
            done=True,
            finish_reason=finish,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
