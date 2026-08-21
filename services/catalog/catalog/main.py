"""Catalog service — owns what a restaurant sells, and the menu cache pipeline."""

import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_kafka import (
    AvroSerde,
    EventProducer,
    SchemaRegistry,
    Topic,
    ensure_compacted_topic,
    topic,
)
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from smartfood_outbox import OutboxPoller
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .adapters.cache import RedisCache
from .adapters.identity_grants import IdentityGrantsClient
from .adapters.search import PostgresSearch
from .api.routes import router
from .config import Settings
from .db import metadata, outbox
from .domain.ports import CachePort, GrantsPort, SearchPort
from .domain.service import CatalogService


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
    grants: GrantsPort | None = None,
    cache: CachePort | None = None,
    search: SearchPort | None = None,
    poller: OutboxPoller | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("catalog")
    setup_tracing("catalog", settings.otlp_endpoint)

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)

    # DI seams: tests inject fake ports; production builds the real adapters
    # (and owns their clients' lifecycles in the lifespan below).
    own_http: httpx.AsyncClient | None = None
    if grants is None:
        own_http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.internal_timeout_seconds, connect=settings.internal_connect_timeout_seconds
            )
        )
        grants = IdentityGrantsClient(settings.identity_base_url, own_http)
    own_redis: aioredis.Redis | None = None
    if cache is None:
        own_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        cache = RedisCache(own_redis)

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    changes_topic = topic(settings.cell_id, Topic.CATALOG_CHANGES)
    own_producer: EventProducer | None = None
    if poller is None and settings.outbox_mode == "poller":  # pragma: no cover
        # Real Kafka wiring — exercised by the live smoke, not the unit suite.
        own_producer = EventProducer(
            settings.kafka_bootstrap, AvroSerde(SchemaRegistry(settings.schema_registry_url))
        )
        poller = OutboxPoller(
            sessions, outbox, topic=changes_topic, producer=own_producer, cell_id=settings.cell_id
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            # Alembic manages its own (sync) connection; run it off the event loop.
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        drain_task: asyncio.Task[None] | None = None
        if poller is not None:
            if own_producer is not None:  # pragma: no cover — live path
                await ensure_compacted_topic(settings.kafka_bootstrap, changes_topic)
                await own_producer.start()
            drain_task = asyncio.create_task(poller.run())
        yield
        if drain_task is not None:
            drain_task.cancel()  # cancellation IS the poller's shutdown signal
            with suppress(asyncio.CancelledError):
                await drain_task
        if own_producer is not None:  # pragma: no cover — live path
            await own_producer.stop()
        await engine.dispose()
        if own_http is not None:
            await own_http.aclose()
        if own_redis is not None:
            await own_redis.aclose()

    app = FastAPI(title="catalog", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    mount_observability(app, engine=engine)

    # Composition root: repo ← service ← routes. The API layer only ever
    # sees app.state.service; the domain only sees the sessionmaker.
    if search is None:
        search = PostgresSearch(sessions)  # PG-only SQL — fine: prod IS Postgres
    app.state.service = CatalogService(sessions, grants, cache, search)

    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "catalog"}

    return app


app = create_app()
