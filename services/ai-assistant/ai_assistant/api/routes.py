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
from ..domain.ports import EmbeddingUnavailable, LlmRateLimited, LlmUnavailable
from ..domain.retrieval import Candidate, Filters
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


class RetrieveIn(StrictModel):
    """`city` is required and has no default: every query is geo-scoped
    (FR-63), and an unscoped retrieval would offer dishes from a city the
    customer cannot order from."""

    query: str = Field(min_length=1, max_length=400)
    city: str = Field(min_length=1, max_length=80)
    max_price_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    cuisines: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=10, ge=1, le=50)
    # The caller states its own policy on paused restaurants, and the
    # default is the strict one. FR-63's "open branches only" governs what
    # the assistant RECOMMENDS — offering a closed kitchen in a chat answer
    # is wrong. Catalog's `/v1/search` is a different contract: its card
    # carries `status` so the client can badge a closed restaurant and still
    # let a customer browse the menu. Hard-coding the strict rule here made
    # a config flag silently change what search returned.
    open_only: bool = True


@router.post("/v1/internal/assistant/retrieve")
async def retrieve(body: RetrieveIn, ctx: SystemOnly, request: Request) -> dict:
    """Ranked candidates for the knowledge index — ids and scores, never
    cards (FR-62).

    SystemOnly and never routed through the edge: the gateway's allowlist
    does not carry `/v1/internal/*`, so this is reachable from inside the
    mesh only. Catalog's `HybridSearch` adapter is its first caller.

    **It returns ids because it must.** Names and prices are Catalog's to
    give, and resolving them there rather than here is what keeps a stale
    indexed `price_cents` structurally unable to reach a customer through
    search — the same rule FR-60 applies to the assistant's answers, bought
    here for free rather than enforced by review.

    Retrieval failing is a 503 with `Retry-After`, never a 500: the caller's
    correct response is to fall back to lexical search (FR-65), and an error
    it can recognise is what lets it.
    """
    retriever = request.app.state.retriever
    filters = Filters(
        city=body.city,
        max_price_cents=body.max_price_cents,
        tags=body.tags,
        cuisines=body.cuisines,
        open_only=body.open_only,
    )
    try:
        found = await retriever.retrieve(query=body.query, filters=filters, limit=body.limit)
    except EmbeddingUnavailable as exc:
        raise _unavailable() from exc
    return {
        "items": [_candidate(c) for c in found.items],
        "restaurants": [_candidate(c) for c in found.restaurants],
    }


def _candidate(candidate: Candidate) -> dict:
    return {
        "chunk_id": candidate.chunk_id,
        "restaurant_id": candidate.restaurant_id,
        "item_id": candidate.item_id,
        "score": candidate.score,
    }
