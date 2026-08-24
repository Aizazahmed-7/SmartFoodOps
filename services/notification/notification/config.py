"""Notification settings — all overridable by environment (compose sets DATABASE_URL).

No outbox_mode: this service consumes orders/payments events and produces
nothing — the first consumer-only service in the fleet.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

    database_url: str = (
        "postgresql+asyncpg://notification_svc:notification_svc@localhost:5432/notification_db"
    )

    # Event backbone: consume c1.orders.events + c1.payments.events.
    kafka_consumers: Literal["on", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Bell push (S9): empty = off — the FE keeps its 15s poll. Compose's
    # app-env REDIS_URL arms it fleet-wide; channels are namespaced
    # sfo:notify:* beside tracking's sfo:track:*.
    redis_url: str = ""
    stream_ticket_ttl_seconds: int = 60
    stream_heartbeat_seconds: float = 15.0
    # FR-36's jittered lifetime, same reasoning as order tracking.
    stream_lifetime_min_seconds: float = 900.0
    stream_lifetime_max_seconds: float = 1800.0

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
