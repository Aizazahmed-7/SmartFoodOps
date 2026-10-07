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
# impossible. Keyed by NAME, not a synthetic id: user_roles.role stores the
# plain role string, which is also the wire format on the claim.
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
    sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# ── multi-role (review 2026-09-10) ─────────────────────────────────
# Supersedes ADR-0022's single-role assumption. `users.role` OVERWROTE
# customer on promotion, so authorization had to patch the fact back in with
# require_role("customer","restaurant_admin") at every customer-facing
# endpoint — a bug CLAUDE.md records as recurring. A role SET keeps both
# facts, so the gate stops being a thing anyone has to remember.
user_roles = sa.Table(
    "user_roles",
    metadata,
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
    # Same seeded lookup as before: the Role enum stays the authority, and
    # this FK is what finally makes `roles` a real many-to-many target
    # rather than a lookup for a single-valued column.
    sa.Column("role", sa.Text, sa.ForeignKey("roles.name"), primary_key=True),
    sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# Rider profile. The old `users.rider_id` was ALWAYS set to users.id — a
# primary-key duplicate carrying no information. This table is the home
# ADR-0022 deferred for rider-only state (vehicle, documents).
# Operational rider state (status, active_deliveries, offer_lock) stays
# dispatch's DynamoDB truth (ADR-0026) and is NEVER mirrored here — two
# sources of truth for "is this rider online" is the failure to avoid.
riders = sa.Table(
    "riders",
    metadata,
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
    sa.Column("onboarded_at", sa.TIMESTAMP(timezone=True), nullable=False),
)

# The owner grant. user_id as PK expresses one-brand-per-owner HERE, mirroring
# catalog's partial unique index on restaurants — the two databases can
# disagree in principle, and the grant-convergence consumer is what keeps
# them honest.
restaurant_owners = sa.Table(
    "restaurant_owners",
    metadata,
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
    sa.Column("brand_id", sa.Text, nullable=False),  # logical -> catalog.restaurants.id
    sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False),
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

# ONE ROW PER LOGIN, updated in place on rotation (review 2026-09-10).
# Was one row per token grouped by family_id: an active 30-day session wrote
# ~480 rows (a refresh every 15 minutes); now it writes one. `family_id` is
# gone because the row IS the session, and `revoked` went with the
# reuse-detection branch that was its only reader.
#
# Consequence accepted deliberately: overwriting token_sha256 means a
# replayed stolen token is indistinguishable from garbage, so token theft is
# no longer detected — rotation's remaining value is that a stolen token
# stops working at the legitimate user's next refresh.
#
# OWED: nothing prunes expired rows. Growth is per-login rather than
# per-refresh, but only a reaper on expires_at gives it a ceiling.
refresh_tokens = sa.Table(
    "refresh_tokens",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    # Indexed for "sign out everywhere" and for listing a user's sessions —
    # the old schema only indexed family_id, so user_id had no index at all.
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("token_sha256", sa.Text, nullable=False, unique=True),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column("rotated_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
