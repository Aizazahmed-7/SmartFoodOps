"""Order settings — all overridable by environment (compose sets the URLs)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    database_url: str = "postgresql+asyncpg://order_svc:order_svc@localhost:5432/order_db"

    # The one synchronous read order needs: catalog's internal pricing
    # snapshot (service-ownership.md — cache-bypassing, torn-read-safe).
    catalog_base_url: str = "http://localhost:8002"

    # Money knobs for smartfood-pricing (integer cents / basis points).
    delivery_fee_cents: int = 199
    tax_basis_points: int = 825

    # Tests use sqlite + create_all; containers run Alembic migrations (S3).
    create_all: bool = False
