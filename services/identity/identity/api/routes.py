"""API layer — HTTP in, HTTP out. Parses requests, calls the domain service,
maps domain exceptions to status codes. No SQL, no business rules."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from smartfood_api import ApiError, StrictModel
from smartfood_auth import Auth, AuthContext, jwks, require_role

from ..domain.service import (
    AddressLimitReached,
    AddressNotFound,
    GrantConflict,
    IdentityService,
    InvalidCredentials,
    InvalidRefreshToken,
    NothingToUpdate,
    RefreshTokenReused,
    UnknownUser,
)

router = APIRouter()


def _svc(request: Request) -> IdentityService:
    return request.app.state.service


class RegisterIn(StrictModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)
    full_name: str | None = Field(default=None, max_length=120)


class LoginIn(StrictModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(max_length=128)


class RefreshIn(StrictModel):
    refresh_token: str = Field(max_length=256)


class TokenPair(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: str
    full_name: str | None
    phone: str | None


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    line1: str
    city: str
    lat: float | None
    lon: float | None


class ProfileUpdate(StrictModel):
    full_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32)


class AddressIn(StrictModel):
    label: str = Field(max_length=40)
    line1: str = Field(max_length=200)
    city: str = Field(max_length=80)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


@router.get("/.well-known/jwks.json")
async def jwks_doc(request: Request) -> dict:
    return jwks([request.app.state.key])


@router.post("/v1/auth/register", status_code=202)
async def register(body: RegisterIn, request: Request) -> dict:
    await _svc(request).register(email=body.email, password=body.password, full_name=body.full_name)
    return {"status": "accepted"}


@router.post("/v1/auth/login")
async def login(body: LoginIn, request: Request) -> TokenPair:
    try:
        pair = await _svc(request).login(email=body.email, password=body.password)
    except InvalidCredentials:
        raise ApiError("AUTH_INVALID_CREDENTIALS", "invalid credentials", 401) from None
    return TokenPair.model_validate(pair)


@router.post("/v1/auth/refresh")
async def refresh(body: RefreshIn, request: Request) -> TokenPair:
    try:
        pair = await _svc(request).refresh(body.refresh_token)
    except RefreshTokenReused:
        raise ApiError(
            "AUTH_REFRESH_REUSED", "refresh token reuse detected — all sessions revoked", 401
        ) from None
    except InvalidRefreshToken:
        raise ApiError("AUTH_INVALID_CREDENTIALS", "invalid refresh token", 401) from None
    return TokenPair.model_validate(pair)


@router.get("/v1/auth/me")
async def me(ctx: Auth, request: Request) -> ProfileOut:
    try:
        profile = await _svc(request).get_profile(ctx.sub)
    except UnknownUser:
        raise ApiError("NOT_FOUND", "unknown user", 404) from None
    return ProfileOut.model_validate(profile)


@router.patch("/v1/auth/me")
async def update_me(body: ProfileUpdate, ctx: Auth, request: Request) -> dict:
    changes = body.model_dump(exclude_none=True)
    try:
        await _svc(request).update_profile(ctx.sub, changes)
    except NothingToUpdate:
        raise ApiError(
            "VALIDATION_FAILED",
            "nothing to update",
            422,
            details=[{"field": "body", "issue": "at least one field required"}],
        ) from None
    except UnknownUser:
        raise ApiError("NOT_FOUND", "unknown user", 404) from None
    return {"status": "updated", **changes}


# ── internal (never in the edge allowlist — unreachable from outside) ──

# require_role() with no roles: ONLY role=system passes (service-to-service).
SystemOnly = Annotated[AuthContext, Depends(require_role())]


class GrantIn(StrictModel):
    user_id: str = Field(max_length=64)
    role: Literal["restaurant_admin"]  # the only grantable role today
    restaurant_id: str = Field(max_length=64)


@router.post("/v1/internal/grants")
async def grant_role(body: GrantIn, ctx: SystemOnly, request: Request) -> dict:
    """Catalog calls this during self-serve onboarding.
    Idempotent — replaying an applied grant succeeds silently."""
    try:
        await _svc(request).grant_restaurant_admin(
            user_id=body.user_id, restaurant_id=body.restaurant_id
        )
    except UnknownUser:
        raise ApiError("NOT_FOUND", "unknown user", 404) from None
    except GrantConflict:
        raise ApiError("GRANT_CONFLICT", "user cannot be granted this restaurant", 409) from None
    return {"status": "granted", "user_id": body.user_id, "restaurant_id": body.restaurant_id}


@router.post("/v1/me/addresses", status_code=201)
async def add_address(body: AddressIn, ctx: Auth, request: Request) -> AddressOut:
    try:
        address = await _svc(request).add_address(ctx.sub, body.model_dump())
    except AddressLimitReached:
        raise ApiError(
            "VALIDATION_FAILED",
            "address limit reached",
            422,
            details=[{"field": "addresses", "issue": "at most 20 saved addresses"}],
        ) from None
    return AddressOut.model_validate(address)


@router.get("/v1/me/addresses")
async def list_addresses(ctx: Auth, request: Request) -> list[AddressOut]:
    return [AddressOut.model_validate(a) for a in await _svc(request).list_addresses(ctx.sub)]


@router.delete("/v1/me/addresses/{address_id}", status_code=204)
async def delete_address(address_id: str, ctx: Auth, request: Request) -> None:
    try:
        await _svc(request).delete_address(ctx.sub, address_id)
    except AddressNotFound:
        raise ApiError("NOT_FOUND", "not found", 404) from None


@router.get("/v1/internal/users/{user_id}/addresses/{address_id}")
async def internal_get_address(
    user_id: str, address_id: str, ctx: SystemOnly, request: Request
) -> AddressOut:
    """Order placement resolves the delivery address server-side (the
    request carries IDs only — never client-asserted address content)."""
    try:
        address = await _svc(request).get_address(user_id, address_id)
    except AddressNotFound:
        raise ApiError("NOT_FOUND", "not found", 404) from None
    return AddressOut.model_validate(address, from_attributes=True)
