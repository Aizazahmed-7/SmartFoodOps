"""The inbox surface: list, mark one read, mark all read.

One endpoint set serves both audiences — the recipient is resolved from
the VERIFIED identity (edge-stamped headers), never from the query: a
partner reads their restaurant's inbox, everyone else their own.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from smartfood_api import ApiError, ErrorCode
from smartfood_auth import AuthContext, Role, require_role

from ..domain.service import NotificationService

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
