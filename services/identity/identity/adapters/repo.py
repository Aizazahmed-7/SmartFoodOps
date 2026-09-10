"""Persistence layer — the ONLY module that writes SQL against identity_db.

A repo is a thin, stateless gateway bound to one session. It contains no
business rules and makes no transaction decisions: commit/rollback belongs
to the domain layer, which knows which writes must survive which errors.
"""

import uuid
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import addresses, refresh_tokens, restaurant_owners, riders, user_roles, users


def _insert_ignoring_conflict(
    table: sa.Table, values: dict[str, Any], conflict_cols: list[str], dialect: str
) -> Any:
    """Both dialects the suite runs on support ON CONFLICT DO NOTHING —
    which is why this replaces try/except IntegrityError: PG aborts the whole
    transaction on a constraint error and sqlite forgives it, and grants are
    replayed routinely (the seed, the convergence consumer)."""
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    return insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict_cols)


class IdentityRepo:
    def __init__(self, session: AsyncSession):
        self._s = session

    @property
    def _dialect(self) -> str:
        return self._s.bind.dialect.name if self._s.bind is not None else "sqlite"

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
        now: datetime,
    ) -> str:
        user_id = f"usr_{uuid.uuid4().hex}"
        await self._s.execute(
            users.insert().values(
                id=user_id,
                email=email,
                password_hash=password_hash,
                full_name=full_name,
                created_at=now,
            )
        )
        return user_id

    async def update_user(self, user_id: str, changes: dict[str, Any]) -> int:
        result = await self._s.execute(
            users.update().where(users.c.id == user_id).values(**changes)
        )
        return cast(CursorResult[Any], result).rowcount

    # ── roles (many-to-many) ───────────────────────────────────────

    async def get_roles(self, user_id: str) -> frozenset[str]:
        rows = (
            await self._s.execute(
                sa.select(user_roles.c.role).where(user_roles.c.user_id == user_id)
            )
        ).scalars()
        return frozenset(rows)

    async def add_role(self, user_id: str, role: str, now: datetime) -> None:
        """Idempotent: re-granting a held role is a no-op, which is what the
        seed and the grant-convergence consumer both rely on."""
        await self._s.execute(
            _insert_ignoring_conflict(
                user_roles,
                {"user_id": user_id, "role": role, "granted_at": now},
                ["user_id", "role"],
                self._dialect,
            )
        )

    # ── role-specific state ────────────────────────────────────────
    # NOTE: "a riders row exists iff the rider role is held" is an invariant
    # these two tables cannot declare — nothing stops one without the other.
    # Both are written in the same transaction as their role grant, which is
    # what keeps them in step.

    async def add_rider(self, user_id: str, now: datetime) -> None:
        await self._s.execute(
            _insert_ignoring_conflict(
                riders, {"user_id": user_id, "onboarded_at": now}, ["user_id"], self._dialect
            )
        )

    async def get_owner_brand(self, user_id: str) -> str | None:
        return (
            await self._s.execute(
                sa.select(restaurant_owners.c.brand_id).where(
                    restaurant_owners.c.user_id == user_id
                )
            )
        ).scalar_one_or_none()

    async def set_owner_brand(self, user_id: str, brand_id: str, now: datetime) -> None:
        """Upsert, not insert-if-absent: a DIFFERENT brand for an existing
        owner is a REPOINT (ADR-0028's cutover moved every owner's scope),
        and every caller is SystemOnly, so last-writer-wins is safe."""
        insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
        statement = insert(restaurant_owners).values(
            user_id=user_id, brand_id=brand_id, granted_at=now
        )
        await self._s.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id"], set_={"brand_id": brand_id, "granted_at": now}
            )
        )

    # ── refresh tokens (one row per LOGIN) ─────────────────────────

    async def get_refresh_by_hash(self, token_sha256: str) -> Row[Any] | None:
        return (
            await self._s.execute(
                sa.select(refresh_tokens).where(refresh_tokens.c.token_sha256 == token_sha256)
            )
        ).one_or_none()

    async def open_session(
        self, *, user_id: str, token_sha256: str, expires_at: datetime, now: datetime
    ) -> str:
        """A LOGIN: one new row. Rotation updates it in place rather than
        appending, so a session's row count stays at one for its lifetime."""
        session_id = f"rt_{uuid.uuid4().hex}"
        await self._s.execute(
            refresh_tokens.insert().values(
                id=session_id,
                user_id=user_id,
                token_sha256=token_sha256,
                expires_at=expires_at,
                created_at=now,
            )
        )
        return session_id

    async def rotate_session(
        self, *, session_id: str, token_sha256: str, expires_at: datetime, now: datetime
    ) -> None:
        """A REFRESH: overwrite the live token on the existing row. The old
        hash is gone, which is why a replayed stolen token now reads as
        garbage rather than as a theft signal (accepted, review 2026-09-10)."""
        await self._s.execute(
            refresh_tokens.update()
            .where(refresh_tokens.c.id == session_id)
            .values(token_sha256=token_sha256, expires_at=expires_at, rotated_at=now)
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

    async def get_address(self, *, user_id: str, address_id: str) -> Row[Any] | None:
        # Ownership lives in the query: wrong owner → None → 404 (docs §5.2).
        result = await self._s.execute(
            sa.select(addresses).where(
                (addresses.c.id == address_id) & (addresses.c.user_id == user_id)
            )
        )
        return result.one_or_none()

    async def delete_address(self, *, user_id: str, address_id: str) -> int:
        # Ownership lives in the query: wrong owner → 0 rows (docs §5.2).
        result = await self._s.execute(
            addresses.delete().where(
                (addresses.c.id == address_id) & (addresses.c.user_id == user_id)
            )
        )
        return cast(CursorResult[Any], result).rowcount
