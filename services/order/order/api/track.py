"""SSE order tracking (FR-36 transport, FR-38 auth).

Two endpoints, two trust models, on purpose:

  POST /v1/track/ticket   — arrives THROUGH the edge (JWT verified, identity
                            stamped). Checks ownership, sells a 60s single-
                            use ticket. This is where auth happens.
  GET  /v1/track/{id}     — arrives DIRECT from the gateway (/sse/track/*),
                            bypassing the edge entirely. The ticket IS the
                            auth: EventSource cannot set headers, and a JWT
                            in a query string would soak into access logs.
                            GETDEL redemption makes replay structurally
                            impossible rather than merely forbidden.

The stream pushes STATUS HINTS. The FE treats each as "refetch now"; the
database stays the only rendered truth, which is what lets the bus fail
open and the stream die freely — the poll fallback is always beneath it.
Connection lifetime is jittered (FR-36) so a fleet's reconnects spread
instead of thundering; the client reopens with a fresh ticket.
"""

import asyncio
import random
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from smartfood_api import ApiError, ErrorCode, StrictModel
from smartfood_auth import AuthContext, Role, require_role

from ..domain.service import OrderNotFound, OrderService

router = APIRouter()

Purchaser = Annotated[AuthContext, Depends(require_role(Role.CUSTOMER, Role.RESTAURANT_ADMIN))]

TERMINAL = {"SETTLED", "CANCELLED", "REFUNDED"}


class TrackingPort(Protocol):
    async def put_ticket(self, ticket: str, order_id: str, sub: str, *, ttl_s: int) -> None: ...
    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None: ...
    def subscription(self, order_id: str) -> Any: ...  # async CM yielding .next_status()


@dataclass(frozen=True)
class TrackingConfig:
    ticket_ttl_s: int = 60
    heartbeat_s: float = 15.0
    lifetime_min_s: float = 900.0
    lifetime_max_s: float = 1800.0
    rng: Callable[[float, float], float] = random.uniform


def _tracking(request: Request) -> TrackingPort | None:
    return request.app.state.tracking


def _config(request: Request) -> TrackingConfig:
    return request.app.state.tracking_config


def _svc(request: Request) -> OrderService:
    return request.app.state.service


class TicketIn(StrictModel):
    order_id: str = Field(min_length=1, max_length=64)


@router.post("/v1/track/ticket", status_code=201)
async def issue_ticket(body: TicketIn, ctx: Purchaser, request: Request) -> dict[str, Any]:
    tracking = _tracking(request)
    if tracking is None:
        # No bus configured: the FE keeps its polling loop — tracking is an
        # enhancement, and its absence must read as "unavailable", not 500.
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "live tracking unavailable",
            503,
            headers={"Retry-After": "30"},
        )
    try:
        await _svc(request).get_order(ctx.sub, body.order_id)  # ownership: not-yours → 404
    except OrderNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "no such order", 404) from None
    ticket = secrets.token_urlsafe(24)
    cfg = _config(request)
    await tracking.put_ticket(ticket, body.order_id, ctx.sub, ttl_s=cfg.ticket_ttl_s)
    return {
        "ticket": ticket,
        "expires_in": cfg.ticket_ttl_s,
        "stream": f"/sse/track/{body.order_id}",
    }


def _sse(status: str) -> str:
    return f"event: status\ndata: {status}\n\n"


async def _stream(
    order_id: str,
    first_status: str,
    tracking: TrackingPort,
    cfg: TrackingConfig,
) -> AsyncIterator[str]:
    yield _sse(first_status)
    if first_status in TERMINAL:
        return  # nothing further will ever happen; let the client settle
    deadline = asyncio.get_running_loop().time() + cfg.rng(cfg.lifetime_min_s, cfg.lifetime_max_s)
    async with tracking.subscription(order_id) as sub:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                # Jittered lifetime reached (FR-36): tell the client to come
                # back with a fresh ticket rather than silently EOFing.
                yield "event: reconnect\ndata: lifetime\n\n"
                return
            try:
                async with asyncio.timeout(min(cfg.heartbeat_s, remaining)):
                    status = await sub.next_status()
            except TimeoutError:
                yield ": hb\n\n"  # SSE comment — keeps proxies from reaping us
                continue
            if status is None:
                continue  # bus poll tick with nothing to say
            yield _sse(status)
            if status in TERMINAL:
                return


@router.get("/v1/track/{order_id}")
async def stream_order(
    order_id: str, request: Request, ticket: Annotated[str, Query(min_length=1)]
) -> StreamingResponse:
    tracking = _tracking(request)
    if tracking is None:
        raise ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, "live tracking unavailable", 503)
    claim = await tracking.consume_ticket(ticket)
    if claim is None or claim.get("order_id") != order_id:
        # Burned either way: a mismatched ticket is consumed too — a probe
        # learns nothing and loses its ticket doing so.
        raise ApiError(ErrorCode.AUTH_INVALID_CREDENTIALS, "invalid or spent ticket", 401)
    order = await _svc(request).get_order(str(claim.get("sub", "")), order_id)
    return StreamingResponse(
        _stream(order_id, str(order["status"]), tracking, _config(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
