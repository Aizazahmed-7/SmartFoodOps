"""Catalog settings — all overridable by environment (compose sets DATABASE_URL)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+asyncpg://catalog_svc:catalog_svc@localhost:5432/catalog_db"
    redis_url: str = "redis://localhost:6379/0"

    # Internal grant call on self-serve onboarding (service-ownership.md: Catalog → Identity).
    identity_base_url: str = "http://localhost:8001"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
