"""Persistence layer — the ONLY module that writes SQL against identity_db.

A repo is a thin, stateless gateway bound to one session. It contains no
business rules and makes no transaction decisions: commit/rollback belongs
to the domain layer, which knows which writes must survive which errors.
"""

import uuid
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import addresses, refresh_tokens, users


class IdentityRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    # ── users ──────────────────────────────────────────────────────

    async def get_user_by_email(self, email: str) -> Row[Any] | None:
        return (await self._s.execute(sa.select(users).where(users.c.email == email))).one_or_none()

    async def get_user_by_id(self, user_id: str) -> Row[Any] | None:
        return (await self._s.execute(sa.select(users).where(users.c.id == user_id))).one_or_none()

    async def insert_user(
        self,
        *,
        email: str,
        password_hash: str,
        full_name: str | None,
        role: str,
        now: datetime,
    ) -> str:
        user_id = f"usr_{uuid.uuid4().hex}"
        await self._s.execute(
            users.insert().values(
                id=user_id,
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                role=role,
                created_at=now,
            )
        )
        return user_id

    async def update_user(self, user_id: str, changes: dict[str, Any]) -> int:
        result = await self._s.execute(
            users.update().where(users.c.id == user_id).values(**changes)
        )
        return cast(CursorResult[Any], result).rowcount

    # ── refresh tokens ─────────────────────────────────────────────

    async def get_refresh_by_hash(self, token_sha256: str) -> Row[Any] | None:
        return (
            await self._s.execute(
                sa.select(refresh_tokens).where(refresh_tokens.c.token_sha256 == token_sha256)
            )
        ).one_or_none()

    async def insert_refresh(
        self,
        *,
        family_id: str,
        user_id: str,
        token_sha256: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        await self._s.execute(
            refresh_tokens.insert().values(
                id=f"rt_{uuid.uuid4().hex}",
                family_id=family_id,
                user_id=user_id,
                token_sha256=token_sha256,
                expires_at=expires_at,
                created_at=now,
            )
        )

    async def mark_rotated(self, token_id: str, now: datetime) -> None:
        await self._s.execute(
            refresh_tokens.update().where(refresh_tokens.c.id == token_id).values(rotated_at=now)
        )

    async def revoke_family(self, family_id: str) -> None:
        await self._s.execute(
            refresh_tokens.update()
            .where(refresh_tokens.c.family_id == family_id)
            .values(revoked=True)
        )

    # ── addresses ──────────────────────────────────────────────────

    async def insert_address(self, *, user_id: str, data: dict[str, Any], now: datetime) -> str:
        address_id = f"adr_{uuid.uuid4().hex}"
        await self._s.execute(
            addresses.insert().values(id=address_id, user_id=user_id, created_at=now, **data)
        )
        return address_id

    async def count_addresses(self, user_id: str) -> int:
        return (
            await self._s.execute(
                sa.select(sa.func.count())
                .select_from(addresses)
                .where(addresses.c.user_id == user_id)
            )
        ).scalar_one()

    async def list_addresses(self, user_id: str) -> list[Row[Any]]:
        return list(
            (
                await self._s.execute(
                    sa.select(addresses)
                    .where(addresses.c.user_id == user_id)
                    .order_by(addresses.c.created_at)
                )
            ).all()
        )

    async def delete_address(self, *, user_id: str, address_id: str) -> int:
        # Ownership lives in the query: wrong owner → 0 rows (docs §5.2).
        result = await self._s.execute(
            addresses.delete().where(
                (addresses.c.id == address_id) & (addresses.c.user_id == user_id)
            )
        )
        return cast(CursorResult[Any], result).rowcount
