"""HTTP surface — DTOs with bounds, envelope error codes, no business logic.

Scoping rule (docs §5.2): a restaurant_admin's claim IS their restaurant id;
a mismatch is a 404, never a 403 — no existence leaks. system/system_admin
bypass scoping (every admin mutation will gain an audit row with W3 logging).
"""

import re
from typing import Annotated
from zoneinfo import available_timezones

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field, field_validator, model_validator
from smartfood_api import ApiError, ErrorCode, StrictModel
from smartfood_auth import AuthContext, Role, require_role, require_system

from ..domain.models import Restaurant
from ..domain.ports import GrantRejected, GrantUnavailable
from ..domain.service import (
    CatalogService,
    CategoryNotEmpty,
    CategoryNotFound,
    ItemNotFound,
    NothingToUpdate,
    RestaurantNotFound,
)

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


def _valid_timezone(v: str) -> str:
    """IANA zone names only. Validating at the write boundary is what lets
    is_open_at treat an unknown zone as a data bug it may degrade around
    (open) rather than a case it must reason about."""
    if v not in available_timezones():
        raise ValueError("unknown IANA timezone")
    return v


class RestaurantCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=64)
    cuisines: list[str] = Field(min_length=1, max_length=5)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    hours: dict[str, list[str]] | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("cuisines")
    @classmethod
    def _normalize_cuisines(cls, v: list[str]) -> list[str]:
        return _slugify(v)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str | None) -> str | None:
        return None if v is None else _valid_timezone(v)

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
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("cuisines")
    @classmethod
    def _normalize_cuisines(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _slugify(v)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str | None) -> str | None:
        return None if v is None else _valid_timezone(v)


class RestaurantOut(StrictModel):
    id: str
    name: str
    city: str
    cuisines: list[str]
    status: str
    lat: float | None
    lon: float | None
    hours: dict[str, list[str]] | None
    timezone: str
    version: int


def _unknown_restaurant() -> ApiError:
    """Not-found and not-yours share one shape (no existence leaks) — and
    one author, so seven call sites cannot drift apart."""
    return ApiError(ErrorCode.NOT_FOUND, "unknown restaurant", 404)


def _nothing_to_update() -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "nothing to update",
        422,
        details=[{"field": "body", "issue": "at least one field required"}],
    )


def _svc(request: Request) -> CatalogService:
    return request.app.state.service


# system_admin is named explicitly (UC-15 admin CRUD): require_role only
# auto-passes `system`, and _own() then exempts both from restaurant scoping.
RestaurantAdmin = Annotated[
    AuthContext, Depends(require_role(Role.RESTAURANT_ADMIN, Role.SYSTEM_ADMIN))
]

_SCOPE_EXEMPT = {"system", "system_admin"}


def _own(ctx: AuthContext, restaurant_id: str) -> None:
    if ctx.role not in _SCOPE_EXEMPT and ctx.restaurant_id != restaurant_id:
        raise _unknown_restaurant()


def _out(restaurant: Restaurant) -> RestaurantOut:
    return RestaurantOut.model_validate(restaurant, from_attributes=True)


# Customers onboard; restaurant_admins must ALSO pass — a successfully
# granted owner replaying the POST (the idempotent path) arrives with their
# promoted role, and the service returns their existing restaurant. Riders
# stay excluded (Identity would refuse the grant anyway — fail at the gate,
# not after committing a restaurant). Found by the seed tool's real-chain
# test; the unit tests' hand-stamped headers had hidden it.
Onboarder = Annotated[AuthContext, Depends(require_role(Role.CUSTOMER, Role.RESTAURANT_ADMIN))]


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
            timezone=body.timezone,
        )
    except GrantRejected:
        raise ApiError(ErrorCode.GRANT_CONFLICT, "onboarding grant rejected", 409) from None
    except GrantUnavailable:
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
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
        raise _unknown_restaurant() from None
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
        raise _nothing_to_update() from None
    except RestaurantNotFound:
        raise _unknown_restaurant() from None
    return _out(restaurant)


async def _set_status(
    restaurant_id: str, status: str, ctx: AuthContext, request: Request
) -> RestaurantOut:
    _own(ctx, restaurant_id)
    try:
        restaurant = await _svc(request).set_status(restaurant_id, status)
    except RestaurantNotFound:
        raise _unknown_restaurant() from None
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


# ── menu: categories ───────────────────────────────────────────────


class CategoryIn(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    rank: int = Field(default=0, ge=0)


class CategoryUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    rank: int | None = Field(default=None, ge=0)


@router.post("/v1/restaurants/{restaurant_id}/categories", status_code=201)
async def add_category(
    restaurant_id: str, body: CategoryIn, ctx: RestaurantAdmin, request: Request
) -> dict:
    _own(ctx, restaurant_id)
    try:
        return await _svc(request).add_category(restaurant_id, name=body.name, rank=body.rank)
    except RestaurantNotFound:
        raise _unknown_restaurant() from None


@router.patch("/v1/restaurants/{restaurant_id}/categories/{category_id}")
async def update_category(
    restaurant_id: str,
    category_id: str,
    body: CategoryUpdate,
    ctx: RestaurantAdmin,
    request: Request,
) -> dict:
    _own(ctx, restaurant_id)
    try:
        return await _svc(request).update_category(
            restaurant_id, category_id, body.model_dump(exclude_none=True)
        )
    except NothingToUpdate:
        raise _nothing_to_update() from None
    except CategoryNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown category", 404) from None


@router.delete("/v1/restaurants/{restaurant_id}/categories/{category_id}")
async def delete_category(
    restaurant_id: str, category_id: str, ctx: RestaurantAdmin, request: Request
) -> dict:
    _own(ctx, restaurant_id)
    try:
        return await _svc(request).delete_category(restaurant_id, category_id)
    except CategoryNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown category", 404) from None
    except CategoryNotEmpty:
        raise ApiError(ErrorCode.CATEGORY_NOT_EMPTY, "category still contains items", 409) from None


# ── menu: items ────────────────────────────────────────────────────


class ModifierOptionIn(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    price_delta_cents: int = Field(default=0, ge=-100_000, le=100_000)
    rank: int = Field(default=0, ge=0)


class ModifierGroupIn(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    min_select: int = Field(default=0, ge=0)
    max_select: int = Field(default=1, ge=1)
    rank: int = Field(default=0, ge=0)
    options: list[ModifierOptionIn] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _satisfiable(self):
        # An unsatisfiable group would make its item unorderable at pricing.
        if self.min_select > self.max_select:
            raise ValueError("min_select exceeds max_select")
        if self.min_select > len(self.options):
            raise ValueError("min_select exceeds the number of options")
        return self


class ItemIn(StrictModel):
    category_id: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    price_cents: int = Field(ge=0, le=10_000_000)  # integer cents, never floats
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    available: bool = True  # create 86'd (False) for the safe-launch pattern
    rank: int = Field(default=0, ge=0)
    tags: list[str] = Field(default_factory=list, max_length=10)
    modifier_groups: list[ModifierGroupIn] = Field(default_factory=list, max_length=10)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: list[str]) -> list[str]:
        return _slugify(v)


class ItemUpdate(StrictModel):
    """Omitted field = untouched. tags/modifier_groups: sending a list
    (even []) replaces the whole set — detected via model_fields_set."""

    category_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    price_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    available: bool | None = None  # the 86 toggle
    rank: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list, max_length=10)
    modifier_groups: list[ModifierGroupIn] = Field(default_factory=list, max_length=10)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: list[str]) -> list[str]:
        return _slugify(v)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # exclude_unset semantics: omitted = untouched, so an EXPLICIT null
        # is only meaningful for description (clearing it) — anywhere else
        # it would reach SQL as a NOT NULL violation (a 500, not a 422).
        for field in self.model_fields_set:
            if field != "description" and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


@router.post("/v1/restaurants/{restaurant_id}/items", status_code=201)
async def add_item(
    restaurant_id: str, body: ItemIn, ctx: RestaurantAdmin, request: Request
) -> dict:
    _own(ctx, restaurant_id)
    fields = body.model_dump(exclude={"category_id", "tags", "modifier_groups"})
    try:
        return await _svc(request).add_item(
            restaurant_id,
            category_id=body.category_id,
            fields=fields,
            tags=body.tags,
            modifier_groups=[g.model_dump() for g in body.modifier_groups],
        )
    except CategoryNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown category", 404) from None


@router.patch("/v1/restaurants/{restaurant_id}/items/{item_id}")
async def update_item(
    restaurant_id: str,
    item_id: str,
    body: ItemUpdate,
    ctx: RestaurantAdmin,
    request: Request,
) -> dict:
    _own(ctx, restaurant_id)
    sent = body.model_fields_set
    changes = body.model_dump(exclude_unset=True, exclude={"tags", "modifier_groups"})
    try:
        return await _svc(request).update_item(
            restaurant_id,
            item_id,
            changes=changes,
            tags=body.tags if "tags" in sent else None,
            modifier_groups=(
                [g.model_dump() for g in body.modifier_groups]
                if "modifier_groups" in sent
                else None
            ),
        )
    except NothingToUpdate:
        raise _nothing_to_update() from None
    except ItemNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown item", 404) from None
    except CategoryNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown category", 404) from None


@router.delete("/v1/restaurants/{restaurant_id}/items/{item_id}")
async def delete_item(
    restaurant_id: str, item_id: str, ctx: RestaurantAdmin, request: Request
) -> dict:
    _own(ctx, restaurant_id)
    try:
        return await _svc(request).delete_item(restaurant_id, item_id)
    except ItemNotFound:
        raise ApiError(ErrorCode.NOT_FOUND, "unknown item", 404) from None


# ── internal (never in the edge allowlist — unreachable from outside) ──

SystemOnly = Annotated[AuthContext, Depends(require_system())]


@router.get("/v1/internal/restaurants/{restaurant_id}/snapshot")
async def pricing_read(
    restaurant_id: str,
    ctx: SystemOnly,
    request: Request,
    item_ids: Annotated[list[str], Query()],
) -> dict:
    """Authoritative pricing read for Order's pricing library (money path).
    Computes a consistent view; persists NOTHING — the durable pricing
    snapshot lives in order_db. Bypasses every cache by design."""
    if not 1 <= len(item_ids) <= 50:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "item_ids out of bounds",
            422,
            details=[{"field": "item_ids", "issue": "between 1 and 50 ids"}],
        )
    try:
        return await _svc(request).pricing_read(restaurant_id, item_ids)
    except RestaurantNotFound:
        raise _unknown_restaurant() from None


# ── public reads: menu (cache-aside) + browse (60s pages) ──────────


@router.get("/v1/menus/{restaurant_id}")
async def get_menu(
    restaurant_id: str,
    request: Request,
    response: Response,
) -> dict:
    """Always the current menu (cache-aside behind the service), so the
    HTTP layer must stay near-fresh — no long client caching."""
    try:
        menu = await _svc(request).get_menu(restaurant_id)
    except RestaurantNotFound:
        raise _unknown_restaurant() from None
    # Browse telemetry (S8) — AFTER the read succeeded (a 404 is not a
    # view), fire-and-forget. The viewer may be anonymous: public_read GETs
    # arrive with no stamped identity, and the funnel counts them toward
    # volume only. X-Auth-Sub is trustworthy here because only the edge can
    # set it (the gateway strips client-supplied copies).
    browse = getattr(request.app.state, "browse", None)
    if browse is not None:
        ctxvars = structlog.contextvars.get_contextvars()
        await browse.menu_viewed(
            restaurant_id,
            user_id=request.headers.get("x-auth-sub"),
            request_id=str(ctxvars.get("request_id", "")) or f"anon-{id(request)}",
        )
    response.headers["Cache-Control"] = "public, max-age=5"
    return menu


def _norm_filter(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    slug = "-".join(value.strip().lower().split())
    if not _SLUG.match(slug):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "invalid filter",
            422,
            details=[{"field": name, "issue": "not a valid tag"}],
        )
    return slug


@router.get("/v1/search")
async def search(
    request: Request,
    response: Response,
    q: Annotated[str, Query(min_length=2, max_length=80)],
    city: str | None = None,
    cuisine: str | None = None,
    tag: str | None = None,
    page: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Fuzzy search over restaurant and item names (ADR-0019). Deliberately
    uncached: q is unbounded-cardinality, the GIN indexes are the answer."""
    result = await _svc(request).search(
        query=q.strip(),
        city="-".join(city.strip().lower().split()) if city is not None else None,
        cuisine=_norm_filter(cuisine, "cuisine"),
        tag=_norm_filter(tag, "tag"),
        page=page,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/v1/restaurants")
async def browse(
    request: Request,
    response: Response,
    city: Annotated[str, Query(min_length=1, max_length=64)],
    cuisine: str | None = None,
    tag: str | None = None,
    page: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    result = await _svc(request).browse(
        city="-".join(city.strip().lower().split()),
        cuisine=_norm_filter(cuisine, "cuisine"),
        tag=_norm_filter(tag, "tag"),
        page=page,
    )
    response.headers["Cache-Control"] = "public, max-age=30"  # CDN tier (docs §7 row 4)
    return result
