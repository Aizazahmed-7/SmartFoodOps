"""Inventory settings — all overridable by environment (compose sets DATABASE_URL)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

    database_url: str = (
        "postgresql+asyncpg://inventory_svc:inventory_svc@localhost:5432/inventory_db"
    )

    # Event backbone: produce c1.inventory.events, consume c1.catalog.changes.
    outbox_mode: Literal["poller", "debezium", "off"] = "off"
    kafka_consumers: Literal["on", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Reservations: TTL must comfortably dominate saga duration (plan: reaper
    # race is accepted locally; a guarded release makes a late commit a no-op).
    reservation_ttl_seconds: int = 1800
    reaper_interval_seconds: float = 30.0

    default_capacity: int = 10  # concurrent-order slots until the admin sets one

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
