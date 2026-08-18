"""The roles lookup (ADR-0022): seeded from the enum, pinned in sync.

The pin test IS the design — the reference production system we studied
(web-controller) has a roles table with three mapped role names that can
never resolve to rows, precisely because nothing pins table contents to
the code vocabulary. Here that drift is a red build."""

import sqlalchemy as sa
from fastapi.testclient import TestClient
from identity.db import metadata, roles, seed_roles
from identity.main import create_app
from smartfood_auth import Role
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool


async def _seeded_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    await seed_roles(engine)
    return engine


async def test_roles_table_is_pinned_to_the_enum():
    """Every Role member has a row; no row exists outside the enum. Either
    direction failing means the table and the code vocabulary diverged —
    the exact failure mode this design forbids."""
    engine = await _seeded_engine()
    async with engine.connect() as conn:
        names = set((await conn.execute(sa.select(roles.c.name))).scalars().all())
    assert names == {str(role) for role in Role}


async def test_seeding_is_idempotent_and_only_ever_adds():
    """Re-seeding (every boot does it) changes nothing; the first seed's
    timestamps survive — rows are facts, not refreshable state."""
    engine = await _seeded_engine()
    async with engine.connect() as conn:
        first = {r.name: r.created_at for r in (await conn.execute(sa.select(roles))).all()}
    await seed_roles(engine)
    async with engine.connect() as conn:
        second = {r.name: r.created_at for r in (await conn.execute(sa.select(roles))).all()}
    assert second == first


def test_app_boot_seeds_the_lookup(settings):
    """The lifespan seeds on the create_all path — the app never runs
    against an empty roles table."""
    app = create_app(settings)
    with TestClient(app) as client:
        # Registration exercises users.role's FK against the seeded row.
        resp = client.post(
            "/v1/auth/register",
            json={"email": "pin@example.com", "password": "hunter2hunter2"},
        )
        assert resp.status_code == 202
