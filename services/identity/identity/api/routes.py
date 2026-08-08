"""API layer — HTTP in, HTTP out. Parses requests, calls the domain service,
maps domain exceptions to status codes. No SQL, no business rules."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr
from smartfood_auth import Auth, jwks

from ..domain.service import (
    AddressNotFound,
    IdentityService,
    InvalidCredentials,
    InvalidRefreshToken,
    NothingToUpdate,
    UnknownUser,
)

router = APIRouter()


def _svc(request: Request) -> IdentityService:
    return request.app.state.service


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


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


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = None


class AddressIn(BaseModel):
    label: str
    line1: str
    city: str
    lat: float | None = None
    lon: float | None = None


@router.get("/.well-known/jwks.json")
async def jwks_doc(request: Request) -> dict:
    return jwks([request.app.state.key])


@router.post("/v1/auth/register", status_code=202)
async def register(body: RegisterIn, request: Request) -> dict:
    await _svc(request).register(
        email=body.email, password=body.password, full_name=body.full_name
    )
    return {"status": "accepted"}


@router.post("/v1/auth/login")
async def login(body: LoginIn, request: Request) -> TokenPair:
    try:
        pair = await _svc(request).login(email=body.email, password=body.password)
    except InvalidCredentials:
        raise HTTPException(status_code=401, detail="invalid credentials") from None
    return TokenPair.model_validate(pair)


@router.post("/v1/auth/refresh")
async def refresh(body: RefreshIn, request: Request) -> TokenPair:
    try:
        pair = await _svc(request).refresh(body.refresh_token)
    except InvalidRefreshToken:
        raise HTTPException(status_code=401, detail="invalid refresh token") from None
    return TokenPair.model_validate(pair)


@router.get("/v1/auth/me")
async def me(ctx: Auth, request: Request) -> ProfileOut:
    try:
        profile = await _svc(request).get_profile(ctx.sub)
    except UnknownUser:
        raise HTTPException(status_code=404, detail="unknown user") from None
    return ProfileOut.model_validate(profile)


@router.patch("/v1/auth/me")
async def update_me(body: ProfileUpdate, ctx: Auth, request: Request) -> dict:
    changes = body.model_dump(exclude_none=True)
    try:
        await _svc(request).update_profile(ctx.sub, changes)
    except NothingToUpdate:
        raise HTTPException(status_code=400, detail="nothing to update") from None
    except UnknownUser:
        raise HTTPException(status_code=404, detail="unknown user") from None
    return {"status": "updated", **changes}


@router.post("/v1/me/addresses", status_code=201)
async def add_address(body: AddressIn, ctx: Auth, request: Request) -> AddressOut:
    address = await _svc(request).add_address(ctx.sub, body.model_dump())
    return AddressOut.model_validate(address)


@router.get("/v1/me/addresses")
async def list_addresses(ctx: Auth, request: Request) -> list[AddressOut]:
    return [
        AddressOut.model_validate(a) for a in await _svc(request).list_addresses(ctx.sub)
    ]


@router.delete("/v1/me/addresses/{address_id}", status_code=204)
async def delete_address(address_id: str, ctx: Auth, request: Request) -> None:
    try:
        await _svc(request).delete_address(ctx.sub, address_id)
    except AddressNotFound:
        raise HTTPException(status_code=404, detail="not found") from None
