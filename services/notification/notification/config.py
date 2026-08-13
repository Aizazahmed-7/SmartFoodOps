"""Notification settings — all overridable by environment (compose sets DATABASE_URL).

No outbox_mode: this service consumes orders/payments events and produces
nothing — the first consumer-only service in the fleet.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://notification_svc:notification_svc@localhost:5432/notification_db"
    )

    # Event backbone: consume c1.orders.events + c1.payments.events.
    kafka_consumers: Literal["on", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
