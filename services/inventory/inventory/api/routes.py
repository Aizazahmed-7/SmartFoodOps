"""HTTP surface — DTOs with bounds, envelope error codes, no business logic.

Scoping mirrors catalog: a restaurant_admin's claim IS their restaurant id;
a mismatch is a 404, never a 403 (no existence leaks). system/system_admin
bypass scoping. The reservation API is system-only — saga activities call
it; it is never edge-routed.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from smartfood_api import ApiError, StrictModel
from smartfood_auth import AuthContext, require_role

from ..domain.models import Reservation, ReservationLine
from ..domain.service import (
    AtCapacity,
    InsufficientStock,
    InventoryService,
    StockScopeMismatch,
)

router = APIRouter()

RestaurantAdmin = Annotated[AuthContext, Depends(require_role("restaurant_admin", "system_admin"))]
SystemOnly = Annotated[AuthContext, Depends(require_role())]  # only role=system passes

_SCOPE_EXEMPT = {"system", "system_admin"}


def _svc(request: Request) -> InventoryService:
    return request.app.state.service


def _own(ctx: AuthContext, restaurant_id: str) -> None:
    if ctx.role not in _SCOPE_EXEMPT and ctx.restaurant_id != restaurant_id:
        raise ApiError("NOT_FOUND", "unknown restaurant", 404)


# ── DTOs ───────────────────────────────────────────────────────────


class StockSet(StrictModel):
    available: int = Field(ge=0, le=100_000)


class CapacitySet(StrictModel):
    capacity: int = Field(ge=1, le=1000)


class LineIn(StrictModel):
    item_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=50)


class ReservationCreate(StrictModel):
    order_id: str = Field(min_length=1, max_length=64)
    restaurant_id: str = Field(min_length=1, max_length=64)
    lines: list[LineIn] = Field(min_length=1, max_length=50)
    ttl_seconds: int | None = Field(default=None, ge=60, le=7200)


class ReleaseIn(StrictModel):
    reason: Literal["cancelled", "expired"]


def _reservation_out(reservation: Reservation) -> dict:
    return {
        "order_id": reservation.order_id,
        "restaurant_id": reservation.restaurant_id,
        "status": reservation.status,
        "lines": [{"item_id": li.item_id, "qty": li.qty} for li in reservation.lines],
    }


# ── restaurant admin: stock & capacity ─────────────────────────────


@router.get("/v1/inventory/restaurants/{restaurant_id}/stock")
async def list_stock(restaurant_id: str, ctx: RestaurantAdmin, request: Request) -> dict:
    _own(ctx, restaurant_id)
    rows = await _svc(request).list_stock(restaurant_id)
    return {
        "items": [
            {"item_id": r.item_id, "available": r.available, "version": r.version} for r in rows
        ]
    }


@router.put("/v1/inventory/restaurants/{restaurant_id}/stock/{item_id}")
async def set_stock(
    restaurant_id: str, item_id: str, body: StockSet, ctx: RestaurantAdmin, request: Request
) -> dict:
    _own(ctx, restaurant_id)
    try:
        row = await _svc(request).set_stock(restaurant_id, item_id, body.available)
    except StockScopeMismatch:
        raise ApiError("NOT_FOUND", "unknown item", 404) from None
    return {"item_id": row.item_id, "available": row.available, "version": row.version}


@router.put("/v1/inventory/restaurants/{restaurant_id}/capacity")
async def set_capacity(
    restaurant_id: str, body: CapacitySet, ctx: RestaurantAdmin, request: Request
) -> dict:
    _own(ctx, restaurant_id)
    capacity, active = await _svc(request).set_capacity(restaurant_id, body.capacity)
    return {"restaurant_id": restaurant_id, "capacity": capacity, "active": active}


# ── system: the reservation lifecycle (saga activities) ────────────


@router.post("/v1/internal/reservations")
async def create_reservation(
    body: ReservationCreate, ctx: SystemOnly, request: Request, response: Response
) -> dict:
    try:
        reservation, created = await _svc(request).reserve(
            order_id=body.order_id,
            restaurant_id=body.restaurant_id,
            lines=[ReservationLine(item_id=li.item_id, qty=li.qty) for li in body.lines],
            ttl_seconds=body.ttl_seconds,
        )
    except AtCapacity:
        raise ApiError(
            "RESTAURANT_AT_CAPACITY", "restaurant cannot take more orders right now", 409
        ) from None
    except InsufficientStock as exc:
        raise ApiError(
            "ITEM_UNAVAILABLE",
            "insufficient stock",
            409,
            details=[
                {"item_id": item_id, "issue": "insufficient stock"} for item_id in exc.item_ids
            ],
        ) from None
    response.status_code = 201 if created else 200
    return _reservation_out(reservation)


@router.post("/v1/internal/reservations/{order_id}/release")
async def release_reservation(
    order_id: str, body: ReleaseIn, ctx: SystemOnly, request: Request
) -> dict:
    changed = await _svc(request).release(order_id, reason=body.reason)
    return {"order_id": order_id, "released": changed}  # False = idempotent no-op


@router.post("/v1/internal/reservations/{order_id}/commit")
async def commit_reservation(order_id: str, ctx: SystemOnly, request: Request) -> dict:
    changed = await _svc(request).commit(order_id)
    return {"order_id": order_id, "committed": changed}
