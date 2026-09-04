"""Bounded HTTP + SSE plumbing shared by the provider adapters.

Retries live here and nowhere else (ADR-0030 §6, anti-pattern #26): no
`tenacity`, no LangGraph checkpointer retry, no loop in a graph node. The
shape is `MockPspClient._post`'s — attempt, classify the status, retry only
what retrying can fix.

`HttpCallFailed` is deliberately vendor-neutral. Each adapter maps it to
its OWN taxonomy in three lines, so `LlmRateLimited` and
`EmbeddingUnavailable` stay meaningful at their ports instead of leaking a
transport type upward.

The vendors are reached over raw httpx rather than their SDKs, for the
reasons the whole repo already reaches third parties this way: an injected
`httpx.AsyncClient` (with `transport=` as the test seam) keeps every wire
shape pinned by a MockTransport test, and keeps pyright's strict tier
satisfiable without depending on a vendor's `py.typed` discipline.
"""

import asyncio
import json as jsonlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    delay_s: float = 0.5


class HttpCallFailed(Exception):
    def __init__(
        self, *, status: int | None, message: str, rate_limited: bool, retryable: bool
    ) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited
        self.retryable = retryable


def classify(status: int, body: str) -> HttpCallFailed:
    """Map a provider status to a retry verdict.

    429 is retryable AND flagged rate-limited: the local retry gives a
    short window for a burst to clear, and the flag is what lets the router
    fail over to another vendor instead of waiting out a quota wall.
    A 4xx that is not 429 is our bug (bad model id, malformed body, dead
    key) — retrying it just makes the same mistake three times.
    """
    detail = body[:200].replace("\n", " ")
    if status == 429:
        return HttpCallFailed(
            status=status,
            message=f"provider rate limited: {detail}",
            rate_limited=True,
            retryable=True,
        )
    if status >= 500:
        return HttpCallFailed(
            status=status,
            message=f"provider {status}: {detail}",
            rate_limited=False,
            retryable=True,
        )
    return HttpCallFailed(
        status=status,
        message=f"provider refused ({status}): {detail}",
        rate_limited=False,
        retryable=False,
    )


async def post_json(
    http: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout_s: float,
    retry: RetryPolicy,
) -> dict[str, Any]:
    last: HttpCallFailed | None = None
    for attempt in range(retry.attempts):
        if attempt:
            await asyncio.sleep(retry.delay_s * attempt)
        try:
            response = await http.post(
                url, headers=dict(headers), json=dict(body), timeout=timeout_s
            )
        except httpx.HTTPError as exc:
            last = HttpCallFailed(
                status=None,
                message=f"provider unreachable: {exc}",
                rate_limited=False,
                retryable=True,
            )
            continue
        if response.status_code == 200:
            decoded: dict[str, Any] = response.json()
            return decoded
        failure = classify(response.status_code, response.text)
        if not failure.retryable:
            raise failure
        last = failure
    assert last is not None  # attempts >= 1, so a failure path always set this
    raise last


async def open_stream(
    http: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout_s: float,
    retry: RetryPolicy,
) -> httpx.Response:
    """Open a streaming POST, retrying only the HANDSHAKE.

    Once a 200 is returned the caller owns the response and must `aclose()`
    it. Retries stop at that point by construction, which matches the
    router's rule: after the first frame reaches a reader, nothing may be
    retried or failed over (ModelRouter.stream).
    """
    last: HttpCallFailed | None = None
    for attempt in range(retry.attempts):
        if attempt:
            await asyncio.sleep(retry.delay_s * attempt)
        request = http.build_request(
            "POST", url, headers=dict(headers), json=dict(body), timeout=timeout_s
        )
        try:
            response = await http.send(request, stream=True)
        except httpx.HTTPError as exc:
            last = HttpCallFailed(
                status=None,
                message=f"provider unreachable: {exc}",
                rate_limited=False,
                retryable=True,
            )
            continue
        if response.status_code == 200:
            return response
        text = (await response.aread()).decode(errors="replace")
        await response.aclose()
        failure = classify(response.status_code, text)
        if not failure.retryable:
            raise failure
        last = failure
    assert last is not None
    raise last


async def sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded `data:` payloads from an SSE response.

    Both vendors put everything we need in `data:` — Anthropic's frames
    carry their own `type` field, so the `event:` line is redundant, and
    OpenAI sends no event names at all. Comments, blank separators and
    OpenAI's `[DONE]` sentinel are dropped here so neither adapter has to
    know about SSE framing. A payload that is not JSON is skipped rather
    than fatal: a provider emitting a keep-alive we have not seen before
    must not end a user's answer.
    """
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            payload = jsonlib.loads(raw)
        except jsonlib.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload
