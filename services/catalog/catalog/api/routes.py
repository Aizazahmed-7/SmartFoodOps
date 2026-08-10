"""HTTP surface — DTOs with bounds, envelope error codes, no business logic.

Scoping rule (docs §5.2): a restaurant_admin's claim IS their restaurant id;
a mismatch is a 404, never a 403 — no existence leaks. system/system_admin
bypass scoping (every admin mutation will gain an audit row with W3 logging).
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, field_validator
from smartfood_api import ApiError, StrictModel
from smartfood_auth import AuthContext, require_role

from ..domain.models import Restaurant
from ..domain.ports import GrantRejected, GrantUnavailable
from ..domain.service import CatalogService, NothingToUpdate, RestaurantNotFound

router = APIRouter()

_SLUG = re.compile(r"^[a-z0-9-]+$")


def _slugify(values: list[str]) -> list[str]:
    """Normalize tags: 'Middle Eastern ' → 'middle-eastern'. Dedupes, keeps
    order. One canonical form so browse filters can never fragment."""
    out: list[str] = []
    for value in values:
        slug = "-".join(value.strip().lower().split())
        if not _SLUG.match(slug):
            raise ValueError(f"not a valid tag: {value!r}")
        if slug not in out:
            out.append(slug)
    return out


class RestaurantCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=64)
    cuisines: list[str] = Field(min_length=1, max_length=5)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    hours: dict[str, list[str]] | None = None

    @field_validator("cuisines")
    @classmethod
    def _normalize_cuisines(cls, v: list[str]) -> list[str]:
        return _slugify(v)

    @field_validator("city")
    @classmethod
    def _normalize_city(cls, v: str) -> str:
        return "-".join(v.strip().lower().split())


class RestaurantUpdate(StrictModel):
    # status is deliberately absent — pause/resume are explicit actions
    # with their own endpoints and their own event types.
    name: str | None = Field(default=None, min_length=1, max_length=120)
    cuisines: list[str] | None = Field(default=None, min_length=1, max_length=5)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    hours: dict[str, list[str]] | None = None

    @field_validator("cuisines")
    @classmethod
    def _normalize_cuisines(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _slugify(v)


class RestaurantOut(StrictModel):
    id: str
    name: str
    city: str
    cuisines: list[str]
    status: str
    lat: float | None
    lon: float | None
    hours: dict[str, list[str]] | None
    version: int


def _svc(request: Request) -> CatalogService:
    return request.app.state.service


# system_admin is named explicitly (UC-15 admin CRUD): require_role only
# auto-passes `system`, and _own() then exempts both from restaurant scoping.
RestaurantAdmin = Annotated[
    AuthContext, Depends(require_role("restaurant_admin", "system_admin"))
]

_SCOPE_EXEMPT = {"system", "system_admin"}


def _own(ctx: AuthContext, restaurant_id: str) -> None:
    if ctx.role not in _SCOPE_EXEMPT and ctx.restaurant_id != restaurant_id:
        raise ApiError("NOT_FOUND", "unknown restaurant", 404)


def _out(restaurant: Restaurant) -> RestaurantOut:
    return RestaurantOut.model_validate(restaurant, from_attributes=True)


# Only customers onboard (riders/admins would be refused by Identity anyway —
# fail at the gate, not after committing a restaurant). `system` passes for seeds.
Onboarder = Annotated[AuthContext, Depends(require_role("customer"))]


@router.post("/v1/restaurants", status_code=201)
async def create_restaurant(
    body: RestaurantCreate, ctx: Onboarder, request: Request, response: Response
) -> RestaurantOut:
    """Self-serve onboarding. Idempotent by owner: a repeat POST returns the
    existing restaurant (200, not 201) and re-attempts the Identity grant —
    the repair path when a first attempt committed but the grant failed."""
    try:
        restaurant, created = await _svc(request).create_restaurant(
            owner_user_id=ctx.sub,
            name=body.name,
            city=body.city,
            cuisines=body.cuisines,
            lat=body.lat,
            lon=body.lon,
            hours=body.hours,
        )
    except GrantRejected:
        raise ApiError("GRANT_CONFLICT", "onboarding grant rejected", 409) from None
    except GrantUnavailable:
        raise ApiError(
            "DEPENDENCY_UNAVAILABLE",
            "could not finish onboarding — retry to complete it",
            503,
            headers={"Retry-After": "1"},
        ) from None
    if not created:
        response.status_code = 200
    return _out(restaurant)


@router.get("/v1/restaurants/{restaurant_id}")
async def get_restaurant(restaurant_id: str, request: Request) -> RestaurantOut:
    try:
        restaurant = await _svc(request).get_restaurant(restaurant_id)
    except RestaurantNotFound:
        raise ApiError("NOT_FOUND", "unknown restaurant", 404) from None
    return _out(restaurant)


@router.patch("/v1/restaurants/{restaurant_id}")
async def update_restaurant(
    restaurant_id: str, body: RestaurantUpdate, ctx: RestaurantAdmin, request: Request
) -> RestaurantOut:
    _own(ctx, restaurant_id)
    changes = body.model_dump(exclude_none=True)
    cuisines = changes.pop("cuisines", None)
    try:
        restaurant = await _svc(request).update_restaurant(restaurant_id, changes, cuisines)
    except NothingToUpdate:
        raise ApiError(
            "VALIDATION_FAILED", "nothing to update", 422,
            details=[{"field": "body", "issue": "at least one field required"}],
        ) from None
    except RestaurantNotFound:
        raise ApiError("NOT_FOUND", "unknown restaurant", 404) from None
    return _out(restaurant)


async def _set_status(
    restaurant_id: str, status: str, ctx: AuthContext, request: Request
) -> RestaurantOut:
    _own(ctx, restaurant_id)
    try:
        restaurant = await _svc(request).set_status(restaurant_id, status)
    except RestaurantNotFound:
        raise ApiError("NOT_FOUND", "unknown restaurant", 404) from None
    return _out(restaurant)


@router.post("/v1/restaurants/{restaurant_id}/pause")
async def pause_restaurant(
    restaurant_id: str, ctx: RestaurantAdmin, request: Request
) -> RestaurantOut:
    return await _set_status(restaurant_id, "paused", ctx, request)


@router.post("/v1/restaurants/{restaurant_id}/resume")
async def resume_restaurant(
    restaurant_id: str, ctx: RestaurantAdmin, request: Request
) -> RestaurantOut:
    return await _set_status(restaurant_id, "open", ctx, request)
