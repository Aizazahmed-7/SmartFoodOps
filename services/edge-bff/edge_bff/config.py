"""edge-bff settings — upstream URLs and token verification parameters.

issuer/audience must match what Identity stamps into tokens; both sides
default to the same values and compose injects the same envs.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

    identity_base_url: str = "http://localhost:8001"
    catalog_base_url: str = "http://localhost:8002"
    inventory_base_url: str = "http://localhost:8005"
    order_base_url: str = "http://localhost:8006"
    notification_base_url: str = "http://localhost:8008"

    identity_jwks_url: str = "http://localhost:8001/.well-known/jwks.json"
    token_issuer: str = "http://identity:8001"
    token_audience: str = "sfo-api"
    jwks_cache_ttl: float = 600.0

    proxy_timeout_seconds: float = 10.0

    # Rate limiting (S2). Empty redis_url = limiter OFF — the same disarmed
    # convention as otlp_endpoint, so unit suites and collectorless installs
    # never need Redis. Limits are per WINDOW per scope (sub or IP); the
    # defaults are sized far above any legitimate single client (the demo
    # scripts and Postman panel run well under them) while still capping a
    # runaway loop or a credential-stuffing run.
    redis_url: str = ""
    rate_limit_window_seconds: int = 60
    rate_limit_auth_per_window: int = 30
    rate_limit_read_per_window: int = 300
    rate_limit_write_per_window: int = 120
