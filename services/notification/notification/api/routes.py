"""The inbox surface: list, mark one read, mark all read.

One endpoint set serves both audiences — the recipient is resolved from
the VERIFIED identity (edge-stamped headers), never from the query: a
partner reads their restaurant's inbox, everyone else their own.
"""

import secrets
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from smartfood_api import ApiError, ErrorCode
from smartfood_auth import AuthContext, Role, require_role
from smartfood_realtime import StreamConfig, stream_events

from ..domain.service import NotificationService
from ..push import notify_channel

router = APIRouter()

Inbox = Annotated[AuthContext, Depends(require_role(Role.CUSTOMER, Role.RESTAURANT_ADMIN))]


def _svc(request: Request) -> NotificationService:
    return request.app.state.service


def _recipient(ctx: AuthContext) -> tuple[str, str]:
    if ctx.role == Role.RESTAURANT_ADMIN and ctx.restaurant_id:
        return "restaurant", ctx.restaurant_id
    return "customer", ctx.sub


@router.get("/v1/notifications")
async def list_notifications(
    ctx: Inbox,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict:
    recipient_type, recipient_id = _recipient(ctx)
    try:
        return await _svc(request).inbox(recipient_type, recipient_id, limit=limit, cursor=cursor)
    except ValueError:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "malformed cursor",
            422,
            details=[{"field": "cursor", "issue": "not a valid cursor"}],
        ) from None


@router.post("/v1/notifications/read-all")
async def mark_all_read(ctx: Inbox, request: Request) -> dict:
    recipient_type, recipient_id = _recipient(ctx)
    return {"marked": await _svc(request).mark_all_read(recipient_type, recipient_id)}


@router.post("/v1/notifications/{notification_id}/read")
async def mark_read(notification_id: str, ctx: Inbox, request: Request) -> dict:
    recipient_type, recipient_id = _recipient(ctx)
    read_at = await _svc(request).mark_read(recipient_type, recipient_id, notification_id)
    if read_at is None:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown notification", 404)
    return {"id": notification_id, "read_at": read_at}


# ── the live bell (S9): ticket-authed SSE, FR-38's design ──────────


class RealtimePort(Protocol):
    async def put_ticket(self, ticket: str, channel: str, sub: str, *, ttl_s: int) -> None: ...
    async def consume_ticket(self, ticket: str) -> dict[str, Any] | None: ...
    def subscription(self, channel: str) -> Any: ...


def _realtime(request: Request) -> RealtimePort | None:
    return request.app.state.realtime


def _stream_config(request: Request) -> StreamConfig:
    return request.app.state.stream_config


@router.post("/v1/notifications/ticket", status_code=201)
async def issue_ticket(ctx: Inbox, request: Request) -> dict:
    """Sell a 60s single-use ticket for THIS caller's bell channel — the
    identity is the claim, so there is nothing else to check: an owner's
    ticket names their restaurant channel, everyone else their own."""
    realtime = _realtime(request)
    if realtime is None:
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "live notifications unavailable",
            503,
            headers={"Retry-After": "30"},
        )
    recipient_type, recipient_id = _recipient(ctx)
    ticket = secrets.token_urlsafe(24)
    cfg = _stream_config(request)
    await realtime.put_ticket(
        ticket, notify_channel(recipient_type, recipient_id), ctx.sub, ttl_s=cfg.ticket_ttl_s
    )
    return {"ticket": ticket, "expires_in": cfg.ticket_ttl_s, "stream": "/sse/notify"}


@router.get("/v1/notifications/stream")
async def stream_bell(
    request: Request, ticket: Annotated[str, Query(min_length=1)]
) -> StreamingResponse:
    """No path identity on purpose: the CLAIM carries the channel, so this
    endpoint cannot be probed for anyone else's bell — there is nothing to
    put in the URL. Arrives direct from the gateway (/sse/notify), never
    through the edge; the ticket IS the auth."""
    realtime = _realtime(request)
    if realtime is None:
        raise ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, "live notifications unavailable", 503)
    claim = await realtime.consume_ticket(ticket)
    channel = None if claim is None else str(claim.get("channel", ""))
    if not channel or not channel.startswith("sfo:notify:"):
        # A tracking ticket redeemed here is burned and refused — channel
        # claims make cross-lane reuse structurally dead.
        raise ApiError(ErrorCode.AUTH_INVALID_CREDENTIALS, "invalid or spent ticket", 401)
    return StreamingResponse(
        stream_events(channel, realtime, _stream_config(request), event_name="notify"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
