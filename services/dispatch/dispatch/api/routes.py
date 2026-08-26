"""Dispatch's three surfaces, one router.

  internal  — the order-worker's cascade activities (system-only)
  rider     — the courier's own console (Role.RIDER; scoped to the
              VERIFIED rider_id claim — the id never rides the URL)
  customer  — one read: where is my courier (ownership in the lookup)

Lost races arrive here as VALUES from the service and leave as 409s with
ORDER_STATE_CONFLICT — the kitchen surface's idiom: losing a race is an
answer, not an error."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from smartfood_api import ApiError, ErrorCode, StrictModel
from smartfood_auth import Auth, AuthContext, Role, require_role, require_system

from ..domain.service import DispatchService, OrderUnavailable

router = APIRouter()

SystemOnly = Annotated[AuthContext, Depends(require_system())]
Rider = Annotated[AuthContext, Depends(require_role(Role.RIDER))]


def _svc(request: Request) -> DispatchService:
    return request.app.state.service


def _conflict(message: str) -> ApiError:
    return ApiError(ErrorCode.ORDER_STATE_CONFLICT, message, 409)


def _unavailable() -> ApiError:
    return ApiError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "order service unavailable",
        503,
        headers={"Retry-After": "1"},
    )


class Point(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


# ── internal: the cascade's activities ─────────────────────────────


class OfferIn(StrictModel):
    order_id: str = Field(max_length=64)
    user_id: str = Field(max_length=64)
    restaurant_name: str = Field(max_length=120)
    pickup: Point
    dropoff: Point
    attempt: int = Field(ge=1, le=100)
    exclude: list[str] = Field(default_factory=list, max_length=100)


@router.post("/v1/internal/dispatch/offers")
async def find_and_offer(body: OfferIn, ctx: SystemOnly, request: Request) -> dict:
    return await _svc(request).find_and_offer(
        body.order_id,
        user_id=body.user_id,
        restaurant_name=body.restaurant_name,
        pickup=(body.pickup.lat, body.pickup.lon),
        dropoff=(body.dropoff.lat, body.dropoff.lon),
        attempt=body.attempt,
        exclude=set(body.exclude),
    )


class ExpireIn(StrictModel):
    order_id: str = Field(max_length=64)
    offer_id: str = Field(max_length=64)
    rider_id: str = Field(max_length=64)


@router.post("/v1/internal/dispatch/offers/expire")
async def expire_offer(body: ExpireIn, ctx: SystemOnly, request: Request) -> dict:
    return await _svc(request).expire_offer(
        body.order_id, offer_id=body.offer_id, rider_id=body.rider_id
    )


class UnassignIn(StrictModel):
    rider_id: str = Field(max_length=64)


@router.post("/v1/internal/dispatch/orders/{order_id}/unassign")
async def unassign(order_id: str, body: UnassignIn, ctx: SystemOnly, request: Request) -> dict:
    return await _svc(request).unassign_stalled(order_id, rider_id=body.rider_id)


@router.post("/v1/internal/dispatch/orders/{order_id}/cancel")
async def cancel(order_id: str, ctx: SystemOnly, request: Request) -> dict:
    return await _svc(request).cancel(order_id)


# ── rider surface ──────────────────────────────────────────────────


class StatusIn(StrictModel):
    online: bool
    # Required when going ONLINE (the pin that makes you a candidate);
    # ignored when going offline. Enforced in the handler so the 422 can
    # say so in words.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


def _rider_id(ctx: AuthContext) -> str:
    # rider_id rides the VERIFIED claim (identity stamps it at grant);
    # sub is the defensive fallback — for riders they are the same id.
    return ctx.rider_id or ctx.sub


@router.post("/v1/rider/status")
async def set_status(body: StatusIn, ctx: Rider, request: Request) -> dict:
    rider_id = _rider_id(ctx)
    if body.online:
        if body.lat is None or body.lon is None:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "going online requires lat and lon", 422)
        await _svc(request).go_online(rider_id, lat=body.lat, lon=body.lon)
        return {"status": "online"}
    await _svc(request).go_offline(rider_id)
    return {"status": "offline"}


@router.get("/v1/rider/me")
async def rider_me(ctx: Rider, request: Request) -> dict:
    return await _svc(request).me(_rider_id(ctx))


class AcceptIn(StrictModel):
    order_id: str = Field(max_length=64)


@router.post("/v1/rider/offers/{offer_id}/accept", status_code=200)
async def accept_offer(offer_id: str, body: AcceptIn, ctx: Rider, request: Request) -> dict:
    try:
        outcome = await _svc(request).accept_offer(
            _rider_id(ctx), offer_id=offer_id, order_id=body.order_id
        )
    except OrderUnavailable:
        raise _unavailable() from None
    if outcome == "expired":
        raise _conflict("offer expired or reassigned")
    return {"status": "assigned", "order_id": body.order_id}


@router.post("/v1/rider/deliveries/{order_id}/pickup")
async def pickup(order_id: str, ctx: Rider, request: Request) -> dict:
    try:
        outcome = await _svc(request).picked_up(_rider_id(ctx), order_id=order_id)
    except OrderUnavailable:
        raise _unavailable() from None
    if outcome != "ok":
        raise _conflict("delivery is not yours to pick up")
    return {"status": "picked_up"}


@router.post("/v1/rider/deliveries/{order_id}/deliver")
async def deliver(order_id: str, ctx: Rider, request: Request) -> dict:
    try:
        outcome = await _svc(request).delivered(_rider_id(ctx), order_id=order_id)
    except OrderUnavailable:
        raise _unavailable() from None
    if outcome != "ok":
        raise _conflict("delivery is not yours to complete")
    return {"status": "delivered"}


# ── customer surface: the courier dot ──────────────────────────────


@router.get("/v1/deliveries/{order_id}/courier")
async def courier(order_id: str, ctx: Auth, request: Request) -> dict:
    view = await _svc(request).courier_position(order_id, caller_sub=ctx.sub)
    if view is None:
        # Not yours and non-existent are the same 404 — no existence leaks.
        raise ApiError(ErrorCode.NOT_FOUND, "not found", 404)
    return view
