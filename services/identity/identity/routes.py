"""The auth endpoints: register, login, refresh, me, JWKS."""

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from smartfood_auth import Auth, jwks
from smartfood_otel import get_logger

from .db import addresses, refresh_tokens, users
from .security import (
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter()
log = get_logger("identity")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """SQLite (tests) returns naive datetimes; Postgres returns aware. Normalize."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


@router.get("/.well-known/jwks.json")
async def jwks_doc(request: Request) -> dict:
    return jwks([request.app.state.key])


@router.post("/v1/auth/register", status_code=202)
async def register(body: RegisterIn, request: Request) -> dict:
    """Public registration — always role=customer (staff accounts are created
    by onboarding flows). Response is identical whether the email is new or
    already registered: enumeration resistance (docs §5.2)."""
    async with request.app.state.sessions() as session, session.begin():
        exists = await session.scalar(sa.select(users.c.id).where(users.c.email == body.email))
        if exists is None:
            await session.execute(
                users.insert().values(
                    id=f"usr_{uuid.uuid4().hex}",
                    email=body.email,
                    password_hash=hash_password(body.password),
                    full_name=body.full_name,
                    role="customer",
                    created_at=_now(),
                )
            )
            log.info("user registered")
    return {"status": "accepted"}


async def _issue_pair(request: Request, session, user_row) -> TokenPair:
    settings = request.app.state.settings
    access = request.app.state.issuer.issue(
        sub=user_row.id,
        role=user_row.role,
        restaurant_id=user_row.restaurant_id,
        rider_id=user_row.rider_id,
    )
    token, token_hash = new_refresh_token()
    await session.execute(
        refresh_tokens.insert().values(
            id=f"rt_{uuid.uuid4().hex}",
            family_id=f"fam_{uuid.uuid4().hex}",
            user_id=user_row.id,
            token_sha256=token_hash,
            expires_at=_now() + timedelta(days=settings.refresh_ttl_days),
            created_at=_now(),
        )
    )
    return TokenPair(
        access_token=access, refresh_token=token, expires_in=settings.access_ttl_seconds
    )


@router.post("/v1/auth/login")
async def login(body: LoginIn, request: Request) -> TokenPair:
    async with request.app.state.sessions() as session, session.begin():
        row = (
            await session.execute(sa.select(users).where(users.c.email == body.email))
        ).one_or_none()
        password_hash = row.password_hash if row else None
        if not verify_password(password_hash, body.password) or row is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        return await _issue_pair(request, session, row)


@router.post("/v1/auth/refresh")
async def refresh(body: RefreshIn, request: Request) -> TokenPair:
    settings = request.app.state.settings
    # Explicit commits, NOT `session.begin()`: raising HTTPException inside a
    # begin() block rolls the transaction back — which would silently undo the
    # family revocation below. Security writes must survive the error response.
    async with request.app.state.sessions() as session:
        row = (
            await session.execute(
                sa.select(refresh_tokens).where(
                    refresh_tokens.c.token_sha256 == hash_refresh_token(body.refresh_token)
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=401, detail="invalid refresh token")

        if row.revoked or row.rotated_at is not None:
            # Reuse of a rotated token = theft signal → kill the whole family,
            # and COMMIT the kill before raising.
            await session.execute(
                refresh_tokens.update()
                .where(refresh_tokens.c.family_id == row.family_id)
                .values(revoked=True)
            )
            await session.commit()
            log.warning("refresh token reuse detected — family revoked", family=row.family_id)
            raise HTTPException(status_code=401, detail="invalid refresh token")

        if _aware(row.expires_at) < _now():
            raise HTTPException(status_code=401, detail="invalid refresh token")

        await session.execute(
            refresh_tokens.update()
            .where(refresh_tokens.c.id == row.id)
            .values(rotated_at=_now())
        )

        user_row = (
            await session.execute(sa.select(users).where(users.c.id == row.user_id))
        ).one()
        access = request.app.state.issuer.issue(
            sub=user_row.id,
            role=user_row.role,
            restaurant_id=user_row.restaurant_id,
            rider_id=user_row.rider_id,
        )
        token, token_hash = new_refresh_token()
        await session.execute(
            refresh_tokens.insert().values(
                id=f"rt_{uuid.uuid4().hex}",
                family_id=row.family_id,
                user_id=row.user_id,
                token_sha256=token_hash,
                expires_at=_now() + timedelta(days=settings.refresh_ttl_days),
                created_at=_now(),
            )
        )
        await session.commit()
        return TokenPair(
            access_token=access, refresh_token=token, expires_in=settings.access_ttl_seconds
        )


@router.get("/v1/auth/me")
async def me(ctx: Auth, request: Request) -> dict:
    async with request.app.state.sessions() as session:
        row = (
            await session.execute(sa.select(users).where(users.c.id == ctx.sub))
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown user")
        return {
            "id": row.id,
            "email": row.email,
            "role": row.role,
            "full_name": row.full_name,
            "phone": row.phone,
        }


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = None


@router.patch("/v1/auth/me")
async def update_me(body: ProfileUpdate, ctx: Auth, request: Request) -> dict:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="nothing to update")
    async with request.app.state.sessions() as session, session.begin():
        result = await session.execute(
            users.update().where(users.c.id == ctx.sub).values(**changes)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="unknown user")
    return {"status": "updated", **changes}


class AddressIn(BaseModel):
    label: str
    line1: str
    city: str
    lat: float | None = None
    lon: float | None = None


@router.post("/v1/me/addresses", status_code=201)
async def add_address(body: AddressIn, ctx: Auth, request: Request) -> dict:
    address_id = f"adr_{uuid.uuid4().hex}"
    async with request.app.state.sessions() as session, session.begin():
        await session.execute(
            addresses.insert().values(
                id=address_id, user_id=ctx.sub, created_at=_now(), **body.model_dump()
            )
        )
    return {"id": address_id, **body.model_dump()}


@router.get("/v1/me/addresses")
async def list_addresses(ctx: Auth, request: Request) -> list[dict]:
    async with request.app.state.sessions() as session:
        rows = (
            await session.execute(
                sa.select(addresses)
                .where(addresses.c.user_id == ctx.sub)
                .order_by(addresses.c.created_at)
            )
        ).all()
        return [
            {"id": r.id, "label": r.label, "line1": r.line1, "city": r.city,
             "lat": r.lat, "lon": r.lon}
            for r in rows
        ]


@router.delete("/v1/me/addresses/{address_id}", status_code=204)
async def delete_address(address_id: str, ctx: Auth, request: Request) -> None:
    async with request.app.state.sessions() as session, session.begin():
        result = await session.execute(
            addresses.delete().where(
                (addresses.c.id == address_id) & (addresses.c.user_id == ctx.sub)
            )
        )
        # Ownership in the query: someone else's address id → 0 rows → 404,
        # not 403 (no existence leaks — docs §5.2).
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
