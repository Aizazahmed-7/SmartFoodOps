"""Dispatch settings — all overridable by environment.

The first PG-less service in the fleet: dispatch's truth lives in
DynamoDB (ADR-0011 — the conditional write IS the assignment lock), its
candidate index in Redis GEO, and its events go direct to Kafka (no
outbox — there is no SQL transaction to be atomic with; ADR-0026).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export. Empty = tracing off.
    otlp_endpoint: str = ""

    # DynamoDB (LocalStack in dev — compose injects AWS_ENDPOINT_URL).
    aws_endpoint_url: str = ""
    aws_region: str = "us-east-1"
    rider_state_table: str = "sfo_rider_state"
    deliveries_table: str = "sfo_deliveries"
    # Dev posture: the service converges its own tables at startup, the
    # initdb idiom. Prod would own them in IaC; the knob records the seam.
    create_tables: bool = True

    # ADR-0011's :cap — fleet-wide tonight (per-rider caps arrive with
    # order stacking, FR-35). One delivery at a time.
    rider_cap: int = 1

    # Candidate index (Redis GEO + latest-loc/heartbeat) — db 2: catalog's
    # cache owns db 0, the edge limiter db 1; flushing one never clears
    # another. Empty = geo disarmed (unit tests inject fakes).
    redis_url: str = ""

    # FR-28/29 knobs: search radius, the widened radius after 3 misses,
    # and the offer cascade timeouts (first offer, then each subsequent).
    search_radius_km: float = 3.0
    widened_radius_km: float = 6.0
    widen_after_misses: int = 3
    offer_first_timeout_s: float = 15.0
    offer_next_timeout_s: float = 12.0

    # Where courier events (accept/pickup/deliver) are forwarded — the
    # order service signals its own workflows (the kitchen precedent).
    order_base_url: str = "http://localhost:8006"

    # Event backbone: dispatch.events, direct produce (ADR-0026).
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"
    dispatch_events: str = "off"  # "on" in compose — off keeps tests quiet
