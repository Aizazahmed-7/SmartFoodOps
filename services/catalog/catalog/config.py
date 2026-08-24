"""Catalog settings — all overridable by environment (compose sets DATABASE_URL)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

    database_url: str = "postgresql+asyncpg://catalog_svc:catalog_svc@localhost:5432/catalog_db"
    # 6380 = compose host mapping (6379 is taken by another local project);
    # in-container the env var overrides this with redis://redis:6379/0.
    redis_url: str = "redis://localhost:6380/0"

    # The zone a new restaurant's posted hours are read in when the owner
    # does not name one. Wall-clock local is the only reading a restaurateur
    # would accept, so SOME zone must be chosen — this makes it a deployment
    # decision instead of a constant buried in a migration.
    default_timezone: str = "America/Chicago"

    # Internal grant call on self-serve onboarding (service-ownership.md: Catalog → Identity).
    identity_base_url: str = "http://localhost:8001"

    # Event backbone (task #6): compose sets poller mode + in-network URLs.
    outbox_mode: Literal["poller", "debezium", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False

    # Internal HTTP calls: per-attempt total / connect budgets. Payment's
    # total is semantically load-bearing (it is what turns tok_timeout into
    # the ambiguous-outcome case), so it must be tunable without a deploy.
    internal_timeout_seconds: float = 5.0
    internal_connect_timeout_seconds: float = 3.0
