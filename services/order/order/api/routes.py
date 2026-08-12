"""HTTP surface — quote (S2), placement + reads (S3).

The route is a translator: DTOs → domain → responses, with the pricing and
idempotency outcomes mapped onto the api-standards code catalog. No
business logic lives here."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from smartfood_api import ApiError, StrictModel
from smartfood_auth import AuthContext, require_role
from smartfood_idempotency import BodyMismatch, InProgress, Replay, body_hash
from smartfood_pricing import (
    InvalidSelection,
    ItemUnavailable,
    Line,
    MenuVersionChanged,
    RestaurantClosed,
    Selection,
)

from ..domain.ports import (
    AddressNotFound,
    AddressUnavailable,
    RestaurantNotFound,
    SnapshotUnavailable,
)
from ..domain.service import InvalidCursor, OrderNotFound, OrderService, Placed

router = APIRouter()

# Customers AND restaurant_admins: a promoted owner is still a person who
# orders dinner (the same single-role-claim lesson as catalog's onboarding
# gate — found live when a demo owner got 403'd on /v1/quote). Riders stay
# excluded until rider flows exist.
Purchaser = Annotated[AuthContext, Depends(require_role("customer", "restaurant_admin"))]


def _svc(request: Request) -> OrderService:
    return request.app.state.service


class SelectionIn(StrictModel):
    group_id: str = Field(min_length=1, max_length=64)
    option_id: str = Field(min_length=1, max_length=64)


class QuoteLineIn(StrictModel):
    item_id: str = Field(min_length=1, max_length=64)
    qty: int = Field(ge=1, le=50)
    options: list[SelectionIn] = Field(default_factory=list, max_length=10)


class QuoteIn(StrictModel):
    restaurant_id: str = Field(min_length=1, max_length=64)
    lines: list[QuoteLineIn] = Field(min_length=1, max_length=50)


def _to_lines(body_lines: list[QuoteLineIn]) -> list[Line]:
    return [
        Line(
            item_id=line.item_id,
            qty=line.qty,
            options=tuple(
                Selection(group_id=s.group_id, option_id=s.option_id) for s in line.options
            ),
        )
        for line in body_lines
    ]


def _map_pricing_errors(exc: Exception) -> ApiError:
    """The pricing/dependency taxonomy → api-standards codes (shared by
    quote and placement)."""
    if isinstance(exc, RestaurantNotFound):
        return ApiError("NOT_FOUND", "unknown restaurant", 404)
    if isinstance(exc, MenuVersionChanged):
        return ApiError(
            "PRICE_CHANGED",
            "menu changed — re-quote and confirm the new total",
            409,
            details=[{"field": "menu_version", "issue": f"menu is now at version {exc.current}"}],
        )
    if isinstance(exc, RestaurantClosed):
        return ApiError("RESTAURANT_CLOSED", "restaurant is not taking orders", 409)
    if isinstance(exc, ItemUnavailable):
        return ApiError(
            "ITEM_UNAVAILABLE",
            "some items are unavailable",
            409,
            details=[{"item_id": item_id, "issue": "unavailable"} for item_id in exc.item_ids],
        )
    if isinstance(exc, InvalidSelection):
        return ApiError("VALIDATION_FAILED", "invalid modifier selection", 422, details=exc.details)
    assert isinstance(exc, (SnapshotUnavailable, AddressUnavailable))
    return ApiError(
        "DEPENDENCY_UNAVAILABLE",
        "temporarily unavailable",
        503,
        headers={"Retry-After": "1"},
    )


_PRICING_ERRORS = (
    RestaurantNotFound,
    MenuVersionChanged,
    RestaurantClosed,
    ItemUnavailable,
    InvalidSelection,
    SnapshotUnavailable,
    AddressUnavailable,
)


@router.post("/v1/quote")
async def quote(body: QuoteIn, ctx: Purchaser, request: Request) -> dict[str, Any]:
    try:
        priced = await _svc(request).quote(body.restaurant_id, _to_lines(body.lines))
    except _PRICING_ERRORS as exc:
        raise _map_pricing_errors(exc) from None
    return priced.model_dump()


# ── placement + reads (S3) ─────────────────────────────────────────


class PlaceOrderIn(QuoteIn):
    """The quote body + the placement pins: version consent, address by ID,
    card by token. Never prices, never address content (api-standards §3)."""

    menu_version: int = Field(ge=0)
    address_id: str = Field(min_length=1, max_length=64)
    card_token: str = Field(pattern=r"^tok_[a-z0-9_]{2,32}$")


@router.post("/v1/orders", status_code=202)
async def place_order(
    body: PlaceOrderIn,
    ctx: Purchaser,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=64)] = None,
) -> Any:
    if not idempotency_key:
        raise ApiError(
            "VALIDATION_FAILED",
            "Idempotency-Key header is required for placement",
            422,
            details=[{"field": "Idempotency-Key", "issue": "required header"}],
        )
    try:
        outcome = await _svc(request).place(
            user_id=ctx.sub,
            idem_key=idempotency_key,
            request_hash=body_hash(await request.body()),
            restaurant_id=body.restaurant_id,
            menu_version=body.menu_version,
            lines=_to_lines(body.lines),
            address_id=body.address_id,
            card_token=body.card_token,
        )
    except AddressNotFound:
        raise ApiError(
            "NOT_FOUND",
            "unknown address",
            404,
            details=[{"field": "address_id", "issue": "no such saved address"}],
        ) from None
    except _PRICING_ERRORS as exc:
        raise _map_pricing_errors(exc) from None

    if isinstance(outcome, Placed):
        return {"order_id": outcome.order_id, "status": "PLACED"}
    if isinstance(outcome, Replay):
        return JSONResponse(
            status_code=outcome.response_status,
            content=outcome.response_body,
            headers={"Idempotent-Replay": "true"},
        )
    if isinstance(outcome, InProgress):
        raise ApiError(
            "IDEMPOTENCY_IN_PROGRESS",
            "a request with this key is already executing",
            409,
            headers={"Retry-After": "1"},
        )
    assert isinstance(outcome, BodyMismatch)
    raise ApiError("IDEMPOTENCY_KEY_REUSE", "this key was used with a different request body", 422)


@router.get("/v1/orders/{order_id}")
async def get_order(order_id: str, ctx: Purchaser, request: Request) -> dict[str, Any]:
    try:
        return await _svc(request).get_order(ctx.sub, order_id)
    except OrderNotFound:
        # not-found and not-yours are the same 404 (no existence leaks)
        raise ApiError("NOT_FOUND", "unknown order", 404) from None


@router.get("/v1/orders")
async def list_orders(
    ctx: Purchaser,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    try:
        return await _svc(request).list_orders(ctx.sub, limit=limit, cursor=cursor)
    except InvalidCursor:
        raise ApiError(
            "VALIDATION_FAILED",
            "malformed cursor",
            422,
            details=[{"field": "cursor", "issue": "not a valid cursor"}],
        ) from None
