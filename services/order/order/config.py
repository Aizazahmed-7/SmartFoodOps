"""Order settings — all overridable by environment (compose sets the URLs)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+asyncpg://order_svc:order_svc@localhost:5432/order_db"

    # Synchronous reads: catalog's pricing snapshot + identity's address
    # resolution (service-ownership.md).
    catalog_base_url: str = "http://localhost:8002"
    identity_base_url: str = "http://localhost:8001"

    # Money knobs for smartfood-pricing (integer cents / basis points).
    delivery_fee_cents: int = 199
    tax_basis_points: int = 825

    # Event backbone: outbox → c1.orders.events (poller runs in the API
    # process ONLY — single-instance ordering; the worker just stages rows).
    outbox_mode: str = "off"  # poller | off
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations (S3).
    create_all: bool = False
