"""Order settings — all overridable by environment (compose sets the URLs)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off —
    # the default everywhere without a collector, unit tests included.
    otlp_endpoint: str = ""

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
    # Deadline for the saga's PRE-CONFIRMED steps (reserve, authorize,
    # confirm). Must stay well under inventory's 1800s reservation TTL: the
    # reaper releasing stock out from under a still-retrying saga is the
    # interaction this bound exists to prevent. Compensations are unbounded.
    forward_deadline_s: int = 300
    # Dispatch (the real courier — the S6 timer knobs died with it).
    dispatch_base_url: str = "http://localhost:8012"
    offer_first_timeout_s: float = 15.0  # FR-29's cascade windows
    offer_next_timeout_s: float = 12.0
    no_rider_deadline_s: float = 600.0  # FR-32: READY-unassigned → cancel
    no_candidates_retry_s: float = 10.0
    pickup_timeout_s: float = 300.0  # FR-30: ASSIGNED w/o pickup → revoke

    # Money knobs for smartfood-pricing (integer cents / basis points).
    delivery_fee_cents: int = 199
    tax_basis_points: int = 825

    # Event backbone: outbox → c1.orders.events (poller runs in the API
    # process ONLY — single-instance ordering; the worker just stages rows).
    outbox_mode: Literal["poller", "debezium", "off"] = "off"
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    cell_id: str = "c1"

    # How long placement waits for the saga to create the order row before
    # answering PlacementPending (ADR-0023). This is the customer's patience
    # budget inside NFR-2's p95 < 3s, NOT a correctness knob: the workflow is
    # already durable when it expires. Raise it if worker schedule-to-start
    # latency grows; lower it to fail fast toward the pending screen.
    placement_await_seconds: float = 2.0

    # Live tracking (S4): empty = off (unit tests; any install without
    # Redis) — the FE then keeps its polling loop, by design. Compose's
    # app-env REDIS_URL arms it fleet-wide; tracking keys/channels are
    # namespaced sfo:track:/sfo:ticket: so sharing db 0 with catalog's
    # cache is collision-free.
    redis_url: str = ""
    track_ticket_ttl_seconds: int = 60
    track_heartbeat_seconds: float = 15.0
    # FR-36: jittered connection lifetime — reconnects spread, never thunder.
    track_lifetime_min_seconds: float = 900.0
    track_lifetime_max_seconds: float = 1800.0

    # The worker is not a web app, so its /metrics rides a bare
    # prometheus_client HTTP server on this port. 0 disables (unit tests).
    worker_metrics_port: int = 9106
    # Temporal's OWN metrics (schedule-to-start latency — THE worker
    # autoscaling signal, ARCHITECTURE §14) on a second port. 0 disables.
    worker_temporal_metrics_port: int = 9107
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
