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

import secrets
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from smartfood_api import ApiError, ErrorCode, StrictModel
from smartfood_auth import AuthContext, Role, require_role
from smartfood_realtime import StreamConfig, stream_events

from ..domain.service import OrderNotFound, OrderService
from ..tracking import track_channel

router = APIRouter()

Purchaser = Annotated[AuthContext, Depends(require_role(Role.CUSTOMER, Role.RESTAURANT_ADMIN))]

TERMINAL = {"SETTLED", "CANCELLED", "REFUNDED"}


class TrackingPort(Protocol):
    async def put_ticket(self, ticket: str, channel: str, sub: str, *, ttl_s: int) -> None: ...
    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None: ...
    def subscription(self, channel: str) -> Any: ...  # async CM yielding .next_message()


def _tracking(request: Request) -> TrackingPort | None:
    return request.app.state.tracking


def _config(request: Request) -> StreamConfig:
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
    await tracking.put_ticket(ticket, track_channel(body.order_id), ctx.sub, ttl_s=cfg.ticket_ttl_s)
    return {
        "ticket": ticket,
        "expires_in": cfg.ticket_ttl_s,
        "stream": f"/sse/track/{body.order_id}",
    }


@router.get("/v1/track/{order_id}")
async def stream_order(
    order_id: str, request: Request, ticket: Annotated[str, Query(min_length=1)]
) -> StreamingResponse:
    tracking = _tracking(request)
    if tracking is None:
        raise ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, "live tracking unavailable", 503)
    claim = await tracking.consume_ticket(ticket)
    if claim is None or claim.get("channel") != track_channel(order_id):
        # Burned either way: a mismatched ticket is consumed too — a probe
        # learns nothing and loses its ticket doing so. Channel-based claims
        # also make a BELL ticket useless here, structurally.
        raise ApiError(ErrorCode.AUTH_INVALID_CREDENTIALS, "invalid or spent ticket", 401)
    order = await _svc(request).get_order(str(claim.get("sub", "")), order_id)
    return StreamingResponse(
        stream_events(
            track_channel(order_id),
            tracking,
            _config(request),
            event_name="status",
            first=str(order["status"]),
            ends_stream=TERMINAL.__contains__,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
