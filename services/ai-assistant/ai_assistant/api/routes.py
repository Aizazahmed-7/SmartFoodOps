"""ai-assistant HTTP surface.

B0 ships only the internal diagnostic pair — one buffered call and one
streamed call — because that is what proves a provider is reachable, that
failover works, and that tokens actually reach a socket. The customer-
facing chat surface (ticket auth, conversation state, the Redis relay with
seq dedupe) lands in B3.

Both routes are SystemOnly: they name a model tier and spend tokens, so
they are operator tooling, not product. They are never in the edge
allowlist.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from smartfood_api import ApiError, ErrorCode, StrictModel
from smartfood_auth import AuthContext, require_system
from smartfood_realtime import sse_event

from ..domain.budget import BudgetExceeded, PlaneShed
from ..domain.ports import LlmRateLimited, LlmUnavailable
from ..domain.router import NoProviderAvailable, Task
from ..domain.service import AssistantService, Turn
from ..metrics import STREAM_CLOSURES

router = APIRouter()

SystemOnly = Annotated[AuthContext, Depends(require_system())]

STREAM_PREFIX = "/v1/internal/assistant/echo/stream"


def _svc(request: Request) -> AssistantService:
    service: AssistantService = request.app.state.service
    return service


def _shed() -> ApiError:
    """Deliberate refusal, not a breakage: ADMISSION_SHED is the code the
    edge already uses when the platform chooses to stop admitting work."""
    return ApiError(
        ErrorCode.ADMISSION_SHED,
        "assistant generation is shed",
        503,
        headers={"Retry-After": "30"},
    )


def _unavailable() -> ApiError:
    return ApiError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "no model provider is available",
        503,
        headers={"Retry-After": "10"},
    )


def _budget() -> ApiError:
    return ApiError(
        ErrorCode.RATE_LIMITED,
        "token budget exhausted for this subject",
        429,
        headers={"Retry-After": "60"},
    )


class EchoIn(StrictModel):
    prompt: str = Field(min_length=1, max_length=4000)
    task: Task = Task.GENERATE


@router.post("/v1/internal/assistant/echo")
async def echo(body: EchoIn, ctx: SystemOnly, request: Request) -> dict:
    """Buffered round trip — proves the port, the router and token accounting."""
    service = _svc(request)
    try:
        turn = await service.prepare(subject=ctx.sub, prompt=body.prompt, task=body.task)
        completion = await service.complete(turn)
    except PlaneShed as exc:
        raise _shed() from exc
    except BudgetExceeded as exc:
        raise _budget() from exc
    except (NoProviderAvailable, LlmUnavailable, LlmRateLimited) as exc:
        raise _unavailable() from exc
    return {
        "text": completion.text,
        "model": completion.model,
        "provider": completion.provider,
        "finish_reason": completion.finish_reason,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
    }


async def _frames(
    service: AssistantService, turn: Turn, *, lifetime_s: float
) -> AsyncIterator[str]:
    """SSE frames for one generation.

    Token payloads are JSON-encoded, not raw. Model output contains
    newlines, and a newline inside an SSE `data:` line silently ends the
    field — raw text would corrupt every multi-line answer. This is also
    the one place in the fleet where a stream carries a PAYLOAD rather than
    a hint (ADR-0034): there is no GET to refetch a half-written sentence
    from, so the frames themselves are the product.

    `sse_event` stays the only frame formatter (smartfood-realtime), so the
    wire shape matches the tracking and bell lanes byte for byte.
    """
    reason = "error"
    try:
        async with asyncio.timeout(lifetime_s):
            async for chunk in service.stream(turn):
                if chunk.done:
                    reason = "complete"
                    yield sse_event(
                        "done",
                        json.dumps(
                            {
                                "finish_reason": chunk.finish_reason,
                                "prompt_tokens": chunk.prompt_tokens,
                                "completion_tokens": chunk.completion_tokens,
                            }
                        ),
                    )
                elif chunk.text:
                    yield sse_event("token", json.dumps(chunk.text))
    except TimeoutError:
        # The safety lifetime exists to reap a WEDGED provider, not to
        # rebalance a fleet — hence 120s, not the tracking lane's 15-30min.
        reason = "lifetime"
        yield sse_event("error", json.dumps("lifetime"))
    except (GeneratorExit, asyncio.CancelledError):
        reason = "client_disconnect"
        raise
    except (NoProviderAvailable, LlmUnavailable, LlmRateLimited):
        yield sse_event("error", json.dumps("provider_unavailable"))
    finally:
        STREAM_CLOSURES.labels(reason=reason).inc()


@router.post(STREAM_PREFIX)
async def echo_stream(body: EchoIn, ctx: SystemOnly, request: Request) -> StreamingResponse:
    """Streamed round trip — proves tokens reach a socket incrementally.

    Admission failures are refused BEFORE the response starts, so they are
    ordinary status codes; anything that fails after the first frame can
    only be reported inside the stream.
    """
    service = _svc(request)
    try:
        turn = await service.prepare(subject=ctx.sub, prompt=body.prompt, task=body.task)
    except PlaneShed as exc:
        raise _shed() from exc
    except BudgetExceeded as exc:
        raise _budget() from exc
    except NoProviderAvailable as exc:
        raise _unavailable() from exc
    return StreamingResponse(
        _frames(service, turn, lifetime_s=request.app.state.stream_lifetime_s),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
