"""OpenAI Chat Completions behind LlmPort — the secondary generation
provider and the reason `EmbeddingPort` needs no third vendor (ADR-0030).

The vendor differences from Anthropic, all absorbed here so the port stays
one shape: system prompts are an ordinary message role; usage arrives in a
single trailing frame that only appears when `stream_options.include_usage`
is set; and the output cap is `max_completion_tokens`. That parameter name
and the model ids in Settings are the two lines to check against the
vendor's current catalog before a deployment — both are pinned by
MockTransport tests below.
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

PROVIDER = "openai"

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_call",
    "content_filter": "refusal",
}


def _finish(reason: object) -> FinishReason:
    return _FINISH_REASONS.get(str(reason), "stop")


def _raise(exc: HttpCallFailed) -> NoReturn:
    """NoReturn, not None: the callers use `except ... as exc: _raise(exc)`
    and then keep using the response variable. Without NoReturn pyright
    correctly flags it as possibly unbound."""
    if exc.rate_limited:
        raise LlmRateLimited(str(exc)) from exc
    raise LlmUnavailable(str(exc)) from exc


class OpenAiLlm:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http: httpx.AsyncClient,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = http
        self._retry = retry or RetryPolicy()

    @property
    def provider(self) -> str:
        return PROVIDER

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._key}", "content-type": "application/json"}

    def _body(
        self, model: str, messages: Sequence[Message], max_output_tokens: int, stream: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": max_output_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": 0,
        }
        if stream:
            body["stream"] = True
            # Without this the streamed response carries NO usage at all,
            # and the plane's token accounting silently reads zero.
            body["stream_options"] = {"include_usage": True}
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
                f"{self._base}/v1/chat/completions",
                headers=self._headers(),
                body=self._body(model, messages, max_output_tokens, stream=False),
                timeout_s=timeout_s,
                retry=self._retry,
            )
        except HttpCallFailed as exc:
            _raise(exc)
        choices = payload.get("choices") or [{}]
        first = choices[0] if isinstance(choices[0], dict) else {}
        usage = payload.get("usage") or {}
        return Completion(
            text=str((first.get("message") or {}).get("content") or ""),
            finish_reason=_finish(first.get("finish_reason")),
            model=str(payload.get("model", model)),
            provider=PROVIDER,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
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
                f"{self._base}/v1/chat/completions",
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
                if usage := frame.get("usage"):
                    prompt_tokens = int(usage.get("prompt_tokens", 0))
                    completion_tokens = int(usage.get("completion_tokens", 0))
                for choice in frame.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    if text := (choice.get("delta") or {}).get("content"):
                        yield TokenChunk(text=str(text))
                    if choice.get("finish_reason"):
                        finish = _finish(choice["finish_reason"])
        finally:
            await response.aclose()
        yield TokenChunk(
            done=True,
            finish_reason=finish,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
