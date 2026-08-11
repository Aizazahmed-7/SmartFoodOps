"""HTTP surface — S2 scope: POST /v1/quote. Orders CRUD arrives in S3.

The route is a translator: DTO → pricing Lines → domain → PricedOrder out,
with the pricing error taxonomy mapped onto the api-standards code catalog.
No business logic lives here."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from smartfood_api import ApiError, StrictModel
from smartfood_auth import AuthContext, require_role
from smartfood_pricing import (
    InvalidSelection,
    ItemUnavailable,
    Line,
    MenuVersionChanged,
    RestaurantClosed,
    Selection,
)

from ..domain.ports import RestaurantNotFound, SnapshotUnavailable
from ..domain.service import OrderService

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


@router.post("/v1/quote")
async def quote(body: QuoteIn, ctx: Purchaser, request: Request) -> dict[str, Any]:
    try:
        priced = await _svc(request).quote(body.restaurant_id, _to_lines(body.lines))
    except RestaurantNotFound:
        raise ApiError("NOT_FOUND", "unknown restaurant", 404) from None
    except MenuVersionChanged as exc:  # pragma: no cover — quote never pins a
        # version; the guard exists for S3's placement path, tested there.
        raise ApiError(
            "PRICE_CHANGED",
            "menu changed — re-quote",
            409,
            details=[{"field": "menu_version", "issue": f"menu is now at version {exc.current}"}],
        ) from None
    except RestaurantClosed:
        raise ApiError("RESTAURANT_CLOSED", "restaurant is not taking orders", 409) from None
    except ItemUnavailable as exc:
        raise ApiError(
            "ITEM_UNAVAILABLE",
            "some items are unavailable",
            409,
            details=[{"item_id": item_id, "issue": "unavailable"} for item_id in exc.item_ids],
        ) from None
    except InvalidSelection as exc:
        raise ApiError(
            "VALIDATION_FAILED", "invalid modifier selection", 422, details=exc.details
        ) from None
    except SnapshotUnavailable:
        raise ApiError(
            "DEPENDENCY_UNAVAILABLE",
            "pricing temporarily unavailable",
            503,
            headers={"Retry-After": "1"},
        ) from None
    return priced.model_dump()
