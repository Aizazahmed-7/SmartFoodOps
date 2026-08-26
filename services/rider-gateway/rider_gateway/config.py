"""rider-gateway settings — the connection plane's knobs (ADR-0006)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    otlp_endpoint: str = ""

    # WS auth: the gateway verifies JWTs itself — it is a GATEWAY (edge
    # class), the one place besides edge-bff allowed to touch JWKS.
    identity_jwks_url: str = "http://localhost:8001/.well-known/jwks.json"
    token_issuer: str = "http://identity:8001"
    token_audience: str = "sfo-api"
    jwks_cache_ttl: float = 600.0

    # The rider-location keys (db 2 — dispatch's index; the SAME keys, the
    # SAME spellings: see ingest.py's cross-reference comment).
    redis_url: str = ""
    cell_id: str = "c1"

    # FR-27: every ping updates Redis; every Nth ping is downsampled onto
    # Kafka for analytics/history (0.2 Hz at a 1 Hz ping rate).
    location_sample_every: int = 5
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    rider_locations: str = "off"  # "on" in compose — off keeps tests quiet
