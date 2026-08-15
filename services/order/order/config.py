"""Order settings — all overridable by environment (compose sets the URLs)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+asyncpg://order_svc:order_svc@localhost:5432/order_db"

    # Synchronous reads: catalog's pricing snapshot + identity's address
    # resolution (service-ownership.md).
    catalog_base_url: str = "http://localhost:8002"
    identity_base_url: str = "http://localhost:8001"

    # Saga wiring (S5): the worker's activities call these.
    inventory_base_url: str = "http://localhost:8005"
    payment_base_url: str = "http://localhost:8007"
    temporal_address: str = "localhost:7233"
    task_queue: str = "order-tq"
    accept_timeout_s: int = 180  # FR-18's restaurant-decision window
    pickup_delay_s: int = 20  # S6's simulated delivery timers
    dropoff_delay_s: int = 30

    # Money knobs for smartfood-pricing (integer cents / basis points).
    delivery_fee_cents: int = 199
    tax_basis_points: int = 825

    # Event backbone: outbox → c1.orders.events (poller runs in the API
    # process ONLY — single-instance ordering; the worker just stages rows).
    outbox_mode: Literal["poller", "debezium", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Placement sweeper (W3): heal PLACED orders whose saga start died in
    # the commit→start gap. min_age must comfortably exceed a normal
    # placement's own start latency so sweeps of healthy orders stay rare
    # (they'd be no-ops anyway — REJECT_DUPLICATE referees).
    sweeper_interval_seconds: float = 30.0
    sweeper_min_age_seconds: float = 60.0

    # Tests use sqlite + create_all; containers run Alembic migrations (S3).
    create_all: bool = False

    # Internal HTTP calls: per-attempt total / connect budgets. Payment's
    # total is semantically load-bearing (it is what turns tok_timeout into
    # the ambiguous-outcome case), so it must be tunable without a deploy.
    internal_timeout_seconds: float = 5.0
    internal_connect_timeout_seconds: float = 3.0

    # The worker's calls ride Temporal's retry policy, so a longer per-attempt
    # budget is safe; the API's synchronous reads stay on the tighter one.
    worker_http_timeout_seconds: float = 10.0
