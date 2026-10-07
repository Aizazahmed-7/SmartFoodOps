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

from .db import EMBEDDING_DIMENSIONS


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
    # Carries the API version segment (the adapters append only the
    # operation): `.../v1` for OpenAI, `.../v1beta/openai` for Gemini's
    # OpenAI-compat shim, `.../openai/v1` for Groq. One key + base_url arms
    # BOTH the fallback LLM and the embedder, so they must be the same vendor.
    openai_base_url: str = "https://api.openai.com/v1"

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
    #
    # The default comes FROM THE SCHEMA (ADR-0032): the column is
    # `vector(512)`, so asking a provider for a different width would write
    # rows the database cannot store. Overriding this without the matching
    # migration is a deployment error, not a tuning knob.
    embedding_dimensions: int = EMBEDDING_DIMENSIONS

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

    # --- Knowledge pipeline (B1) ----------------------------------------
    kafka_bootstrap: str = "localhost:19092"
    schema_registry_url: str = "http://localhost:8086"
    # Live wiring switch, matching every other consumer service: "off" runs
    # the API with no consumer attached, which is what `make dev` and the
    # unit suite want.
    kafka_consumers: Literal["on", "off"] = "on"
    # The AI plane's facts are a product KPI, so they leave through the
    # outbox like every other business fact (FR-94, ADR-0002/0012) — never
    # direct to Kafka. "off" is the unit suite's default: nothing drains,
    # rows just accumulate, and the staging assertions still hold.
    outbox_mode: Literal["poller", "debezium", "off"] = "off"
    # FR-57's window. A FIXED window from the first unprocessed change, not
    # a sliding one — see PendingRepo.stage. 30 s + embed + upsert is what
    # NFR-28's 60 s p99 freshness budget is made of, so raising this eats
    # that budget directly.
    knowledge_debounce_seconds: float = 30.0
    # How often the drain asks "what is due?". It is a floor on freshness,
    # so it has to stay small relative to the debounce — 5 s spends a
    # trivial indexed query per tick and keeps the 30 s window meaning 30 s
    # rather than 30-to-60.
    knowledge_drain_interval_seconds: float = 5.0
    # Restaurants per pass. A cap rather than a target: it bounds how long
    # one pass can hold the loop when a backlog drains, so a burst cannot
    # starve the retrieval API sharing this event loop.
    knowledge_drain_batch: int = 20

    # --- Rolling reindex (FR-61) ----------------------------------------
    celery_broker_url: str = ""
    # Chunks per embed round trip, and round trips per invocation. The cap
    # keeps one task from holding a worker for an hour on a large corpus:
    # the operator (or a beat schedule) re-runs until `done`, and the work
    # is resumable by construction so re-running is also the retry policy.
    reindex_batch: int = 200
    reindex_max_batches: int = 50

    # --- Retrieval (B2) -------------------------------------------------
    # An OVERRIDE for a specific deployment, not the mechanism: correct
    # recall under a selective filter comes from `hnsw.iterative_scan`, set
    # as superuser in initdb because these are placeholder parameters until
    # pgvector's library loads. None leaves the database default alone.
    hnsw_ef_search: int | None = None

    # --- The turn (B3) --------------------------------------------------
    # How many dishes the model is shown. A prompt is bounded and a
    # retriever is not, so this is a context-cap control as much as a
    # quality one (ADR-0030 §5) — and a model given forty candidates writes
    # a worse answer than one given eight.
    # --- Answer cache (B3, FR-74) ---------------------------------------
    # "off" disables both tiers. The unit suite and `make dev` run without
    # it so a test never has to reason about a warm cache it did not set up.
    answer_cache: Literal["on", "off"] = "on"
    # The bound on how long a staleness the FENCE does not catch survives:
    # a prompt edit, a model swap, a grounding rule tightened. An hour is
    # short enough that nobody ships a fix and waits, long enough that a
    # popular question is answered from cache all lunchtime.
    answer_cache_ttl_s: int = 3600
    # COSINE DISTANCE, so smaller is stricter — 0.12 is roughly "a rephrasing
    # of the same question", not "another question about food". Tuned against
    # the golden set rather than guessed: too loose and the cache answers a
    # question nobody asked, which is a correctness bug wearing a latency
    # improvement's clothes.
    answer_cache_distance: float = 0.12

    # Catalog, for the live card resolution FR-60 requires. The assistant
    # holds the system identity the internal snapshot endpoint needs, which
    # a browser does not — so cards are resolved here, not in the FE.
    catalog_base_url: str = "http://localhost:8002"

    # Inventory, for the kitchen-congestion read the explanation engine
    # needs (FR-85). `active`/`capacity` is the only congestion signal the
    # platform has, and until FR-85 nothing could read it.
    inventory_base_url: str = "http://localhost:8005"

    # Order and Dispatch, for the facts an explanation is built from
    # (FR-83). Both reads are SystemOnly and unscoped — the assistant holds
    # the customer's identity and checks ownership itself.
    order_base_url: str = "http://localhost:8004"
    dispatch_base_url: str = "http://localhost:8012"

    # How many dishes a recommendation surface shows. Smaller than the
    # candidate limit on purpose: candidates are a model's raw material and
    # a recommendation is a list a human reads.
    recommend_limit: int = 5
    # How often taste profiles are rebuilt. Fifteen minutes because a
    # profile is a six-month average — it does not move on the timescale a
    # menu does, and rebuilding it faster would buy nothing but load.
    profile_interval_seconds: float = 900.0
    # How long after being shown a dish an order still counts as accepting
    # it (FR-79). Two hours: long enough for someone to browse, think, and
    # come back; short enough that tomorrow's dinner is not credited to
    # today's suggestion. It is a product judgement rather than a derived
    # number, so it is config — and the rate is meaningless without knowing
    # which window produced it.
    attribution_window_minutes: int = 120

    candidate_limit: int = 8
    # Prior turns carried into the prompt. Bounded for the same reason.
    history_limit: int = 6
