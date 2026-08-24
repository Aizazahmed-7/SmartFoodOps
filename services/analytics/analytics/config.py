"""Analytics settings — all overridable by environment (compose sets DATABASE_URL).

Consumer-fed like notification, but a PROJECTOR, not an inbox: it folds the
order lifecycle into per-order fact rows and answers aggregate questions.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

    database_url: str = (
        "postgresql+asyncpg://analytics_svc:analytics_svc@localhost:5432/analytics_db"
    )

    # Event backbone: consume c1.orders.events in MICRO-BATCHES (FR-43).
    # The payments topic is deliberately NOT consumed: none of the eight
    # buildable metrics needs a payment event — order events carry totals
    # and every lifecycle timestamp. One loop, one group, less to operate.
    kafka_consumers: Literal["on", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # FR-43's micro-batch shape: up to `max` events or `wait_ms`, whichever
    # first, folded in ONE transaction with ONE commit. At Uber-like volume
    # this is the knob that keeps the projector's write amplification flat —
    # per-event commits are the thing that would not survive 35k msg/s.
    batch_max: int = 500
    batch_wait_ms: int = 5000

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False
