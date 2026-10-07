"""ai-assistant settings — the ONLY place this service reads the environment
(anti-pattern #29). Every knob has a local-dev-safe default.

The disarm convention matters more here than anywhere else in the fleet:
an EMPTY provider key disables that adapter (ADR-0030 §7), so the unit
suite needs no credential and no network, and a missing key in a deployed
environment is a loud 503 rather than a mystery. Same shape as
`otlp_endpoint=""` (tracing off) and `redis_url=""` (budgets local-only).
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # OTLP/HTTP collector for span export (Jaeger). Empty = tracing off.
    otlp_endpoint: str = ""

    database_url: str = (
        "postgresql+asyncpg://assistant_svc:assistant_svc@localhost:5432/assistant_db"
    )

    cell_id: str = "c1"

    # Tests use sqlite + create_all; containers run Alembic migrations.
    create_all: bool = False

    # --- Providers (ADR-0030) -------------------------------------------
    # Two adapters from day one because ONE vendor is both a capacity wall
    # (quota is tokens/minute) and a single point of failure. Empty key =>
    # that provider is unregistered; zero registered providers => every
    # generation path answers 503 and the plane runs retrieval-only.
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com"

    # Model ids are DATA, not code — the router maps a task to one of these
    # (ADR-0030 §3) and callers never name a model. Confirm the OpenAI ids
    # against the vendor's current catalog before a deployment; the
    # Anthropic ids are the current generation.
    model_generate: str = "claude-sonnet-5"
    model_cheap: str = "claude-haiku-4-5-20251001"
    model_generate_fallback: str = "gpt-4.1"
    model_cheap_fallback: str = "gpt-4.1-mini"

    embedding_model: str = "text-embedding-3-small"
    # 512 of 1536 available dimensions: these embeddings are Matryoshka-
    # truncatable, so this is a ~3x storage cut for a small relevance cost,
    # measured by the eval suite before it is changed. A change here is a
    # model_version bump and a rolling reindex (PRD FR-61), never in place.
    embedding_dimensions: int = 512

    llm_timeout_s: float = 30.0
    llm_max_attempts: int = 3
    llm_retry_delay_s: float = 0.5

    # --- Budgets (ADR-0030 §5) ------------------------------------------
    # Empty redis_url runs the guard in local-only mode: the per-request
    # context cap still applies (it needs no store), the shared per-user
    # and per-cell limits do not. Correct for tests and single-node dev,
    # never correct in deployment — the readiness note in docs/runbooks.md
    # names it.
    redis_url: str = ""
    user_token_budget: int = 200_000
    user_token_window_s: int = 3600
    cell_token_budget: int = 50_000_000
    cell_token_window_s: int = 60

    # Held-connection SSE knobs, deliberately far shorter than the tracking
    # lane's 15-30 min: a generation lasts seconds, so the safety lifetime
    # exists only to reap a wedged stream, not to rebalance a fleet.
    stream_heartbeat_seconds: float = 15.0
    stream_lifetime_seconds: float = 120.0

    # Generation concurrency cap (ADR-0031): the turn runs as a background
    # task in this process, so this is what stops a chat burst from
    # starving the retrieval API on the same event loop.
    max_concurrent_turns: int = 32

    generation: Literal["on", "off"] = "on"
