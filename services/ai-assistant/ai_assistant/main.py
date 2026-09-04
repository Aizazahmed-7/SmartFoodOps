"""ai-assistant — the GenAI plane (ADR-0029).

Request-shaped and latency-sensitive, so it runs as a container beside the
other domain services rather than as a Lambda (ADR-0008's rule). B0 wires
the providers, the router, the budget guard and the diagnostic routes; the
knowledge consumer (B1) and the Celery lanes (B6) attach as lifespan
runners and separate entrypoints later, the way notification's do.

The injection seams are deliberately mid-grained. Tests pass `providers=`
so they exercise the REAL router, guard and service over a scripted fake
provider — the failover rule and the budget arithmetic are the parts most
worth testing, and a coarse `service=` override would skip both.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from .api.routes import STREAM_PREFIX, router
from .config import Settings
from .db import metadata
from .domain.budget import BudgetGuard, BudgetStore
from .domain.ports import LlmPort
from .domain.router import ModelRouter, default_policy
from .domain.service import AssistantService


def _run_migrations(database_url: str) -> None:  # pragma: no cover — Postgres-only path,
    # exercised by the compose stack, not the sqlite unit suite.
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def create_app(
    settings: Settings | None = None,
    *,
    providers: dict[str, LlmPort] | None = None,
    budget_store: BudgetStore | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("ai-assistant")
    # Must precede every engine/client construction below: setup_tracing
    # arms the httpx and SQLAlchemy instrumentors, so a client built first
    # is a client whose provider calls never appear as spans.
    setup_tracing("ai-assistant", settings.otlp_endpoint)

    engine_kwargs: dict[str, Any] = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)

    own_http: httpx.AsyncClient | None = None
    own_redis: Any | None = None

    if providers is None:  # pragma: no cover — live wiring (compose runs it)
        from .adapters._http import RetryPolicy
        from .adapters.llm_anthropic import AnthropicLlm
        from .adapters.llm_openai import OpenAiLlm

        own_http = httpx.AsyncClient()
        retry = RetryPolicy(attempts=settings.llm_max_attempts, delay_s=settings.llm_retry_delay_s)
        providers = {}
        # An empty key REMOVES a provider from the fleet rather than
        # producing 401s at request time (ADR-0030 §7). Zero registered
        # providers is a legal, deliberate state: every generative path
        # answers 503 and the plane runs retrieval-only.
        if settings.anthropic_api_key:
            providers["anthropic"] = AnthropicLlm(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
                http=own_http,
                api_version=settings.anthropic_version,
                retry=retry,
            )
        if settings.openai_api_key:
            providers["openai"] = OpenAiLlm(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                http=own_http,
                retry=retry,
            )

    if budget_store is None and settings.redis_url:  # pragma: no cover — live wiring
        import redis.asyncio as aioredis

        from .adapters.budget_redis import RedisBudgetStore

        own_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        budget_store = RedisBudgetStore(own_redis)

    policy = default_policy(
        generate_model=settings.model_generate,
        cheap_model=settings.model_cheap,
        generate_fallback=settings.model_generate_fallback,
        cheap_fallback=settings.model_cheap_fallback,
        timeout_s=settings.llm_timeout_s,
    )
    service = AssistantService(
        router=ModelRouter(providers, policy),
        guard=BudgetGuard(
            cell_id=settings.cell_id,
            user_budget=settings.user_token_budget,
            user_window_s=settings.user_token_window_s,
            cell_budget=settings.cell_token_budget,
            cell_window_s=settings.cell_token_window_s,
            store=budget_store,
        ),
        generation=settings.generation,
        max_concurrent_turns=settings.max_concurrent_turns,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        yield
        if own_http is not None:  # pragma: no cover — live wiring
            await own_http.aclose()
        if own_redis is not None:  # pragma: no cover — live wiring
            await own_redis.aclose()
        await engine.dispose()

    app = FastAPI(title="ai-assistant", lifespan=lifespan)
    # Token streams are LIFETIMES, not latencies. Without this the echo
    # stream's full duration lands in http_request_duration_seconds and
    # pins the service p95 at the top bucket — the live incident recorded
    # in smartfood_otel/middleware.py.
    app.add_middleware(RequestContextMiddleware, stream_prefixes=(STREAM_PREFIX,))
    install_error_handlers(app)
    mount_observability(app, engine=engine)
    app.state.service = service
    app.state.stream_lifetime_s = settings.stream_lifetime_seconds
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "ai-assistant"}

    return app


app = create_app()
