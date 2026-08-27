"""The kitchen's HTTP surface — /v1/restaurant/* (S6, edge-routed, auth).

Translation only: claims → scoping, KitchenService outcomes → the
api-standards catalog. The 202-vs-200 split on decisions is the honest
contract: 202 = "your verdict is with the saga" (the workflow applies it),
200 = "already applied — here is the truth". The feed is the kitchen's
poll target either way.

Scoping mirrors inventory: the admin's claim IS their restaurant; a
missing claim or someone else's order is a 404, never a 403.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from smartfood_api import ApiError, ErrorCode
from smartfood_auth import AuthContext, Role, require_role

from ..db import OrderStatus
from ..domain.kitchen import AlreadyDecided, KitchenService, KitchenStateConflict
from ..domain.ports import SagaUnavailable
from ..domain.service import InvalidCursor, OrderNotFound
from ..values import Verdict

router = APIRouter()

RestaurantAdmin = Annotated[AuthContext, Depends(require_role(Role.RESTAURANT_ADMIN))]


def _kitchen(request: Request) -> KitchenService:
    return request.app.state.kitchen


def _claim(ctx: AuthContext) -> str:
    """The admin's restaurant claim is the only scope there is — no claim,
    no restaurant, same 404 as a mismatch (no existence leaks)."""
    if ctx.restaurant_id is None:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown restaurant", 404)
    return ctx.restaurant_id


def _map_kitchen_errors(exc: Exception) -> ApiError:
    if isinstance(exc, OrderNotFound):
        return ApiError(ErrorCode.NOT_FOUND, "unknown order", 404)
    if isinstance(exc, AlreadyDecided):
        return ApiError(ErrorCode.ORDER_ALREADY_DECIDED, str(exc), 409)
    if isinstance(exc, KitchenStateConflict):
        return ApiError(
            ErrorCode.ORDER_STATE_CONFLICT,
            "order is not in a state for this action",
            409,
            details=[{"field": "status", "issue": f"order is {exc.actual}"}],
        )
    assert isinstance(exc, SagaUnavailable)
    return ApiError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "temporarily unavailable",
        503,
        headers={"Retry-After": "1"},
    )


_KITCHEN_ERRORS = (OrderNotFound, AlreadyDecided, KitchenStateConflict, SagaUnavailable)


@router.get("/v1/restaurant/orders")
async def feed(
    ctx: RestaurantAdmin,
    request: Request,
    # Repeated: ?status=CONFIRMED&status=PREPARING — one round trip serves
    # every queue the kitchen page renders (it batches all four and groups
    # client-side). A single value still parses as a one-element list.
    status: Annotated[list[OrderStatus], Query(min_length=1, max_length=8)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    try:
        return await _kitchen(request).feed(
            _claim(ctx), statuses=status, limit=limit, cursor=cursor
        )
    except InvalidCursor:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "malformed cursor",
            422,
            details=[{"field": "cursor", "issue": "not a valid cursor"}],
        ) from None


async def _decide(request: Request, ctx: AuthContext, order_id: str, verdict: Verdict) -> Any:
    try:
        result = await _kitchen(request).decide(_claim(ctx), order_id, verdict)
    except _KITCHEN_ERRORS as exc:
        raise _map_kitchen_errors(exc) from None
    body = {"order_id": order_id, "decision": verdict, "status": result.status}
    return JSONResponse(status_code=202 if result.signalled else 200, content=body)


@router.post("/v1/restaurant/orders/{order_id}/accept")
async def accept(order_id: str, ctx: RestaurantAdmin, request: Request) -> Any:
    return await _decide(request, ctx, order_id, "accept")


@router.post("/v1/restaurant/orders/{order_id}/reject")
async def reject(order_id: str, ctx: RestaurantAdmin, request: Request) -> Any:
    return await _decide(request, ctx, order_id, "reject")


@router.post("/v1/restaurant/orders/{order_id}/preparing")
async def preparing(order_id: str, ctx: RestaurantAdmin, request: Request) -> dict[str, Any]:
    try:
        status = await _kitchen(request).set_preparing(_claim(ctx), order_id)
    except _KITCHEN_ERRORS as exc:
        raise _map_kitchen_errors(exc) from None
    return {"order_id": order_id, "status": status}


@router.post("/v1/restaurant/orders/{order_id}/ready")
async def ready(order_id: str, ctx: RestaurantAdmin, request: Request) -> dict[str, Any]:
    try:
        status = await _kitchen(request).set_ready(_claim(ctx), order_id)
    except _KITCHEN_ERRORS as exc:
        raise _map_kitchen_errors(exc) from None
    return {"order_id": order_id, "status": status}
