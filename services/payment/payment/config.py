"""Payment settings — all overridable by environment (compose sets the URLs)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+asyncpg://payment_svc:payment_svc@localhost:5432/payment_db"

    # The one external dependency: the PSP behind the PaymentGateway port
    # (ADR-0010 — only THIS service ever imports a PSP adapter).
    mock_psp_base_url: str = "http://localhost:9080"

    # Event backbone: outbox → c1.payments.events.
    outbox_mode: str = "off"  # poller | off
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
