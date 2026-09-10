"""Domain layer — the business rules of identity.

No HTTP here (no FastAPI imports, no status codes) and no SQL (that's the
repo's). Failures are domain exceptions; the API layer translates them.
This is also where transactions are DECIDED: a grant writes the role and
its role-specific row in ONE transaction, so the two never disagree.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from smartfood_auth import Role, TokenIssuer
from smartfood_otel import get_logger
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..adapters.repo import IdentityRepo
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
        *,
        access_ttl_seconds: int,
        refresh_ttl_days: int,
    ):
        # Explicit values, not the Settings object: the domain must not
        # depend on how configuration is sourced (env, files, tests).
        self._sessions = sessions
        self._issuer = issuer
        self._access_ttl_seconds = access_ttl_seconds
        self._refresh_ttl_days = refresh_ttl_days

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
                now = _now()
                user_id = await repo.insert_user(
                    email=email,
                    password_hash=password_hash,
                    full_name=full_name,
                    now=now,
                )
                await repo.add_role(user_id, str(Role.CUSTOMER), now)
                await session.commit()
                log.info("user registered")

    async def login(self, *, email: str, password: str) -> TokenPairData:
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            user = await repo.get_user_by_email(email)
            password_hash = user.password_hash if user else None
            if not verify_password(password_hash, password) or user is None:
                raise InvalidCredentials
            pair = await self._issue_pair(repo, user, session_id=None)
            await session.commit()
            return pair

    # ── refresh rotation ───────────────────────────────────────────

    async def refresh(self, refresh_token: str) -> TokenPairData:
        """Rotation UPDATES the session row in place (review 2026-09-10), so
        the old hash is overwritten rather than kept. A replayed stolen token
        therefore reads as an unknown token, not as a theft signal — the
        detection the previous append-per-token model provided is gone by
        decision, and rotation's remaining value is that a stolen token stops
        working at the legitimate holder's next refresh."""
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            row = await repo.get_refresh_by_hash(hash_refresh_token(refresh_token))
            if row is None:
                raise InvalidRefreshToken

            if _aware(row.expires_at) < _now():
                raise InvalidRefreshToken

            user = await repo.get_user_by_id(row.user_id)
            if user is None:
                raise InvalidRefreshToken
            pair = await self._issue_pair(repo, user, session_id=row.id)
            await session.commit()
            return pair

    async def _issue_pair(
        self, repo: IdentityRepo, user: Row[Any], *, session_id: str | None
    ) -> TokenPairData:
        """`session_id=None` is a LOGIN (opens a session row); a value is a
        REFRESH (rotates that row). The claim shape is unchanged — only its
        SOURCE moved: roles come from user_roles, restaurant_id from
        restaurant_owners, and rider_id is derived because the old column was
        always equal to users.id."""
        roles = await repo.get_roles(user.id)
        access = self._issuer.issue(
            sub=user.id,
            roles=roles,
            restaurant_id=await repo.get_owner_brand(user.id),
            rider_id=user.id if str(Role.RIDER) in roles else None,
        )
        token, token_hash = new_refresh_token()
        now = _now()
        expires_at = now + timedelta(days=self._refresh_ttl_days)
        if session_id is None:
            await repo.open_session(
                user_id=user.id, token_sha256=token_hash, expires_at=expires_at, now=now
            )
        else:
            await repo.rotate_session(
                session_id=session_id, token_sha256=token_hash, expires_at=expires_at, now=now
            )
        return TokenPairData(
            access_token=access,
            refresh_token=token,
            expires_in=self._access_ttl_seconds,
        )

    # ── internal grants ────────────────────────────────────────────

    async def grant_restaurant_admin(self, *, user_id: str, restaurant_id: str) -> None:
        """Idempotent: Catalog re-attempts the same grant while repairing a
        half-finished onboarding, so a replay must be a silent success.

        A DIFFERENT restaurant_id for an existing admin is a REPOINT, not a
        conflict (ADR-0028): the brands cutover moves every owner's scope
        from their branch row to the minted brand, delivered through the
        same convergence consumer. Last-writer-wins is safe because every
        caller is SystemOnly and catalog enforces one brand per owner —
        there is no legitimate competing writer. GrantConflict remains for
        role-CLASS violations (riders can't own restaurants) — a restriction
        the single-role column used to impose for free, now explicit."""
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            user = await repo.get_user_by_id(user_id)
            if user is None:
                raise UnknownUser
            roles = await repo.get_roles(user_id)
            if str(Role.RIDER) in roles:
                raise GrantConflict  # riders can't own restaurants
            previous = await repo.get_owner_brand(user_id)
            if previous == restaurant_id:
                return  # replay of an already-applied grant
            now = _now()
            await repo.set_owner_brand(user_id, restaurant_id, now)
            await repo.add_role(user_id, str(Role.RESTAURANT_ADMIN), now)
            await session.commit()
            if previous is None:
                log.info("restaurant_admin granted", user=user_id, restaurant=restaurant_id)
            else:
                log.info(
                    "restaurant_admin scope repointed",
                    user=user_id,
                    restaurant=restaurant_id,
                    previous=previous,
                )

    async def grant_rider(self, *, user_id: str) -> None:
        """Dispatch onboarding (FR-1's rider role). The `riders` row is the
        home for rider-only PROFILE state (vehicle, documents) — operational
        state stays dispatch's DynamoDB truth (ADR-0026) and is never
        mirrored here. Idempotent for the same reason the restaurant grant
        is: the seed replays."""
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            user = await repo.get_user_by_id(user_id)
            if user is None:
                raise UnknownUser
            roles = await repo.get_roles(user_id)
            if str(Role.RIDER) in roles:
                return  # replay of an already-applied grant
            if str(Role.RESTAURANT_ADMIN) in roles:
                raise GrantConflict  # owners don't moonlight as couriers
            now = _now()
            await repo.add_role(user_id, str(Role.RIDER), now)
            await repo.add_rider(user_id, now)
            await session.commit()
            log.info("rider granted", user=user_id)

    # ── profile ────────────────────────────────────────────────────

    async def get_profile(self, user_id: str) -> Profile:
        async with self._sessions() as session:
            repo = IdentityRepo(session)
            user = await repo.get_user_by_id(user_id)
            if user is None:
                raise UnknownUser
            roles = await repo.get_roles(user_id)
            return Profile(
                id=user.id,
                email=user.email,
                roles=tuple(sorted(roles)),
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

    async def get_address(self, user_id: str, address_id: str) -> Address:
        """Internal read for order placement (system callers): the address
        must belong to the given user — a foreign id is a 404, same as
        absent (no existence leaks, even between services)."""
        async with self._sessions() as session:
            row = await IdentityRepo(session).get_address(user_id=user_id, address_id=address_id)
            if row is None:
                raise AddressNotFound
            return Address(
                id=row.id, label=row.label, line1=row.line1, city=row.city, lat=row.lat, lon=row.lon
            )

    async def delete_address(self, user_id: str, address_id: str) -> None:
        async with self._sessions() as session:
            rowcount = await IdentityRepo(session).delete_address(
                user_id=user_id, address_id=address_id
            )
            if rowcount == 0:
                raise AddressNotFound
            await session.commit()
