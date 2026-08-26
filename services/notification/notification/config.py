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

    # Receipts (S10): empty broker = the whole pipeline is disarmed — no
    # Celery enqueue, no receipt rows minted. Compose arms it with
    # amqp://guest:guest@rabbitmq:5672// (dev-fixture creds, the image's
    # defaults). Same idiom as redis_url/otlp_endpoint above.
    celery_broker_url: str = ""
    mailer_base_url: str = "http://localhost:9081"
    mailer_timeout_seconds: float = 5.0
    # Recipient resolution at send time (adapters/contacts.py): events
    # carry no PII, so the worker asks Identity for the CURRENT email.
    identity_base_url: str = "http://localhost:8001"
    contacts_timeout_seconds: float = 5.0
    receipts_bucket: str = "sfo-receipts"
    # Compose injects AWS_ENDPOINT_URL=http://localstack:4566 fleet-wide;
    # empty means real AWS (boto3's default resolution).
    aws_endpoint_url: str = ""
    # The sweeper: how often beat re-enqueues owed-but-unsent receipts, and
    # how old a row must be before it is "owed" (grace covers the window
    # where the post-commit enqueue's chain is still legitimately in flight).
    receipt_sweep_seconds: float = 300.0
    receipt_sweep_grace_seconds: float = 120.0

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
