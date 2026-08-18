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
    # Deadline for the saga's PRE-CONFIRMED steps (reserve, authorize,
    # confirm). Must stay well under inventory's 1800s reservation TTL: the
    # reaper releasing stock out from under a still-retrying saga is the
    # interaction this bound exists to prevent. Compensations are unbounded.
    forward_deadline_s: int = 300
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

    # How long placement waits for the saga to create the order row before
    # answering PlacementPending (ADR-0023). This is the customer's patience
    # budget inside NFR-2's p95 < 3s, NOT a correctness knob: the workflow is
    # already durable when it expires. Raise it if worker schedule-to-start
    # latency grows; lower it to fail fast toward the pending screen.
    placement_await_seconds: float = 2.0

    # How long a reserved-but-uncompleted placement key blocks a retry of the
    # SAME key. The library default is 300s, sized for "a crash mid-
    # transaction — do not re-execute eagerly". Placement no longer needs
    # that caution: the order id is derived from the key (ADR-0023), so a
    # takeover provably converges on the same order and the same workflow
    # instead of minting a second one. Short wins — after a Temporal outage
    # the customer would otherwise be locked out of THAT cart for 5 minutes.
    placement_key_ttl_seconds: int = 30

    # Idempotency-key garbage collection: nothing else reclaims these rows.
    # The janitor sleeps first, so a boot never triggers a table-wide delete
    # and the unit suites never reach a tick. 0 disables it.
    idempotency_purge_interval_seconds: float = 3600.0
    idempotency_orphan_ttl_seconds: float = 3600.0

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
