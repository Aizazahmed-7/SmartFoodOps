"""Domain layer — the business rules of identity.

No HTTP here (no FastAPI imports, no status codes) and no SQL (that's the
repo's). Failures are domain exceptions; the API layer translates them.
This is also where transactions are DECIDED: the family-kill on refresh
reuse commits before its error propagates — the bug class we hit when
these concerns were interleaved.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from smartfood_auth import TokenIssuer
from smartfood_otel import get_logger
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import IdentityRepo
from ..config import Settings
from ..security import (
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from .models import Address, Profile, TokenPairData

log = get_logger("identity.domain")


class IdentityError(Exception):
    """Base for all identity domain failures."""


class InvalidCredentials(IdentityError):
    pass


class InvalidRefreshToken(IdentityError):
    pass


class RefreshTokenReused(InvalidRefreshToken):
    """Rotated/revoked token presented again — family has been revoked.
    Distinct so the API can return AUTH_REFRESH_REUSED: the legitimate
    holder learns their token was stolen."""


class UnknownUser(IdentityError):
    pass


class NothingToUpdate(IdentityError):
    pass


class GrantConflict(IdentityError):
    """User is already scoped to another restaurant, or their role is not
    grantable (riders/admins can't own restaurants)."""


class AddressNotFound(IdentityError):
    pass


class AddressLimitReached(IdentityError):
    """Creation cap: the address list stays small enough that an unpaginated
    read is correct — and a hostile client can't grow the table unbounded."""


MAX_ADDRESSES = 20


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """SQLite (tests) returns naive datetimes; Postgres returns aware. Normalize."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class IdentityService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        issuer: TokenIssuer,
        settings: Settings,
    ):
        self._sessions = sessions
        self._issuer = issuer
        self._settings = settings

    # ── registration & login ───────────────────────────────────────

    async def register(self, *, email: str, password: str, full_name: str | None) -> None:
        """Idempotent: registering an existing email is a silent no-op —
        the caller must not be able to tell (enumeration resistance)."""
        # argon2 costs ~100ms of CPU — do it BEFORE opening the session so no
        # transaction is held during the hash (tx-boundary rule, checklists).
        password_hash = hash_password(password)
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            if await repo.get_user_by_email(email) is None:
                await repo.insert_user(
                    email=email,
                    password_hash=password_hash,
                    full_name=full_name,
                    role="customer",
                    now=_now(),
                )
                await session.commit()
                log.info("user registered")

    async def login(self, *, email: str, password: str) -> TokenPairData:
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            user = await repo.get_user_by_email(email)
            password_hash = user.password_hash if user else None
            if not verify_password(password_hash, password) or user is None:
                raise InvalidCredentials
            pair = await self._issue_pair(repo, user, family_id=None)
            await session.commit()
            return pair

    # ── refresh rotation ───────────────────────────────────────────

    async def refresh(self, refresh_token: str) -> TokenPairData:
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            row = await repo.get_refresh_by_hash(hash_refresh_token(refresh_token))
            if row is None:
                raise InvalidRefreshToken

            if row.revoked or row.rotated_at is not None:
                # Reuse of a rotated token = theft signal → kill the family,
                # and COMMIT the kill before the error propagates.
                await repo.revoke_family(row.family_id)
                await session.commit()
                log.warning("refresh token reuse detected — family revoked", family=row.family_id)
                raise RefreshTokenReused

            if _aware(row.expires_at) < _now():
                raise InvalidRefreshToken

            await repo.mark_rotated(row.id, _now())
            user = await repo.get_user_by_id(row.user_id)
            if user is None:
                raise InvalidRefreshToken
            pair = await self._issue_pair(repo, user, family_id=row.family_id)
            await session.commit()
            return pair

    async def _issue_pair(
        self, repo: IdentityRepo, user: Row[Any], family_id: str | None
    ) -> TokenPairData:
        access = self._issuer.issue(
            sub=user.id,
            role=user.role,
            restaurant_id=user.restaurant_id,
            rider_id=user.rider_id,
        )
        token, token_hash = new_refresh_token()
        await repo.insert_refresh(
            family_id=family_id or f"fam_{uuid.uuid4().hex}",
            user_id=user.id,
            token_sha256=token_hash,
            expires_at=_now() + timedelta(days=self._settings.refresh_ttl_days),
            now=_now(),
        )
        return TokenPairData(
            access_token=access,
            refresh_token=token,
            expires_in=self._settings.access_ttl_seconds,
        )

    # ── internal grants ────────────────────────────────────────────

    async def grant_restaurant_admin(self, *, user_id: str, restaurant_id: str) -> None:
        """Idempotent: Catalog re-attempts the same grant while repairing a
        half-finished onboarding, so a replay must be a silent success."""
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            user = await repo.get_user_by_id(user_id)
            if user is None:
                raise UnknownUser
            if user.role == "restaurant_admin":
                if user.restaurant_id == restaurant_id:
                    return  # replay of an already-applied grant
                raise GrantConflict  # scoped to a different restaurant
            if user.role != "customer":
                raise GrantConflict  # riders/admins can't own restaurants
            await repo.update_user(
                user_id, {"role": "restaurant_admin", "restaurant_id": restaurant_id}
            )
            await session.commit()
            log.info("restaurant_admin granted", user=user_id, restaurant=restaurant_id)

    # ── profile ────────────────────────────────────────────────────

    async def get_profile(self, user_id: str) -> Profile:
        async with self._sessions() as session:
            user = await IdentityRepo(session).get_user_by_id(user_id)
            if user is None:
                raise UnknownUser
            return Profile(
                id=user.id,
                email=user.email,
                role=user.role,
                full_name=user.full_name,
                phone=user.phone,
            )

    async def update_profile(self, user_id: str, changes: dict[str, Any]) -> None:
        if not changes:
            raise NothingToUpdate
        async with self._sessions() as session:
            rowcount = await IdentityRepo(session).update_user(user_id, changes)
            if rowcount == 0:
                raise UnknownUser
            await session.commit()

    # ── addresses ──────────────────────────────────────────────────

    async def add_address(self, user_id: str, data: dict[str, Any]) -> Address:
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            if await repo.count_addresses(user_id) >= MAX_ADDRESSES:
                raise AddressLimitReached
            address_id = await repo.insert_address(user_id=user_id, data=data, now=_now())
            await session.commit()
            return Address(id=address_id, **data)

    async def list_addresses(self, user_id: str) -> list[Address]:
        async with self._sessions() as session:
            rows = await IdentityRepo(session).list_addresses(user_id)
            return [
                Address(id=r.id, label=r.label, line1=r.line1, city=r.city, lat=r.lat, lon=r.lon)
                for r in rows
            ]

    async def delete_address(self, user_id: str, address_id: str) -> None:
        async with self._sessions() as session:
            rowcount = await IdentityRepo(session).delete_address(
                user_id=user_id, address_id=address_id
            )
            if rowcount == 0:
                raise AddressNotFound
            await session.commit()
