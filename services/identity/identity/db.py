"""identity_db schema — the tables from docs/service-ownership.md.

SQLAlchemy Core table objects: the single source the Alembic migration and
the test create_all both derive from.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from smartfood_auth import Role
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = sa.MetaData()

# Roles as a seeded lookup table (ADR-0022, team decision 2026-08-14).
# CONTRACT: the smartfood_auth.Role enum REMAINS the authority — this table
# is seeded from it at startup (idempotently) and a test pins the two in
# sync, so the drift that plagues unsynced role tables is structurally
# impossible. Keyed by NAME, not a synthetic id: users.role stays the plain
# role string (wire format, claims, and gates untouched; no join, ever).
roles = sa.Table(
    "roles",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("email", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("full_name", sa.Text, nullable=True),
    sa.Column("phone", sa.Text, nullable=True),
    # FK to the seeded lookup replaces the old CHECK — same closed set,
    # now enforced referentially (migration 0004 swapped them).
    sa.Column(
        "role", sa.Text, sa.ForeignKey("roles.name"), nullable=False, server_default="customer"
    ),
    sa.Column("restaurant_id", sa.Text, nullable=True),
    sa.Column("rider_id", sa.Text, nullable=True),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

addresses = sa.Table(
    "addresses",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("line1", sa.Text, nullable=False),
    sa.Column("city", sa.Text, nullable=False),
    sa.Column("lat", sa.Float, nullable=True),
    sa.Column("lon", sa.Float, nullable=True),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

refresh_tokens = sa.Table(
    "refresh_tokens",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("family_id", sa.Text, nullable=False, index=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("token_sha256", sa.Text, nullable=False, unique=True),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("rotated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# Consumer-side dedupe (ADR-0018): one row per (group, event) ever processed.
# The grant operation is idempotent anyway — this table suppresses replay
# noise and makes at-least-once delivery observable.
processed_events = sa.Table(
    "processed_events",
    metadata,
    sa.Column("consumer_group", sa.Text, primary_key=True),
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=False),
)


async def seed_roles(engine: AsyncEngine) -> None:
    """Converge the roles lookup to the Role enum — idempotently, at every
    startup (both the create_all test path and the migrated container path).
    Seeding FROM the enum is the anti-drift contract: a new Role member
    appears here on the next boot, and no deploy can forget the INSERT.
    Rows are only ever added — removing a role is a migration-with-a-plan
    (existing users reference these rows by FK)."""
    now = datetime.now(UTC)
    dialect_insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    async with engine.begin() as conn:
        await conn.execute(
            dialect_insert(roles)
            .values([{"name": str(role), "created_at": now} for role in Role])
            .on_conflict_do_nothing(index_elements=["name"])
        )
