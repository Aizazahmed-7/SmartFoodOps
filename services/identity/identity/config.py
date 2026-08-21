"""Identity settings — all overridable by environment (compose sets DATABASE_URL)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

    database_url: str = "postgresql+asyncpg://identity_svc:identity_svc@localhost:5432/identity_db"
    signing_key_path: str = "infra/local/keys/identity-rsa.pem"

    token_issuer: str = "http://identity:8001"
    token_audience: str = "sfo-api"
    access_ttl_seconds: int = 900
    refresh_ttl_days: int = 30

    # Event backbone (task #6): the grant-convergence consumer (ADR-0020).
    kafka_consumers: Literal["on", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
