"""Catalog settings — all overridable by environment (compose sets DATABASE_URL)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+asyncpg://catalog_svc:catalog_svc@localhost:5432/catalog_db"
    # 6380 = compose host mapping (6379 is taken by another local project);
    # in-container the env var overrides this with redis://redis:6379/0.
    redis_url: str = "redis://localhost:6380/0"

    # Internal grant call on self-serve onboarding (service-ownership.md: Catalog → Identity).
    identity_base_url: str = "http://localhost:8001"

    # Event backbone (task #6): compose sets poller mode + in-network URLs.
    outbox_mode: str = "off"  # poller | off
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8081"
    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
