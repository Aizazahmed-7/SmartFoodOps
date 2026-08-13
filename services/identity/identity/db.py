"""identity_db schema — the tables from docs/service-ownership.md.

SQLAlchemy Core table objects: the single source the Alembic migration and
the test create_all both derive from.
"""

import sqlalchemy as sa
from smartfood_auth import ROLES

metadata = sa.MetaData()

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("email", sa.Text, nullable=False, unique=True),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("full_name", sa.Text, nullable=True),
    sa.Column("phone", sa.Text, nullable=True),
    sa.Column("role", sa.Text, nullable=False, server_default="customer"),
    # Derived from the auth lib's closed vocabulary — one source of truth.
    sa.CheckConstraint(f"role IN {tuple(sorted(ROLES))!r}", name="ck_users_role"),
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
