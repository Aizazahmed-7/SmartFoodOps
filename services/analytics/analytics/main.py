"""Analytics service — the metrics projector over c1.orders.events
(docs/service-ownership.md). Consumer-fed like notification, but batched:
FR-43's five-second micro-batch, via smartfood_kafka.run_batches."""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_kafka import AvroSerde, EventConsumer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .api.routes import router
from .config import Settings
from .db import metadata
from .domain.service import AnalyticsService

Runner = Callable[[], Coroutine[Any, Any, None]]


def _run_migrations(database_url: str) -> None:  # pragma: no cover — Postgres-only path,
    # exercised by the compose stack, not the sqlite unit suite.
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def create_app(settings: Settings | None = None, *, runners: list[Runner] | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging("analytics")
    setup_tracing("analytics", settings.otlp_endpoint)

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    live_runners = list(runners) if runners is not None else []
    if not live_runners and settings.kafka_consumers == "on":  # pragma: no cover — live
        # Live wiring only: loop, retries, DLQ policy all live (tested) in
        # smartfood-kafka; the batch shape is FR-43's.
        from .consumers import GROUP_FACTS, FactsProjector

        projector = FactsProjector(sessions)
        consumer = EventConsumer(
            topic(settings.cell_id, Topic.ORDERS_EVENTS),
            GROUP_FACTS,
            projector,  # unused by batch mode, but the constructor wants a handler
            AvroSerde(SchemaRegistry(settings.schema_registry_url)),
            bootstrap=settings.kafka_bootstrap,
        )
        live_runners = [
            lambda: consumer.run_batches(
                projector, max_batch=settings.batch_max, wait_ms=settings.batch_wait_ms
            )
        ]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        tasks = [asyncio.create_task(runner()) for runner in live_runners]
        yield
        for task in tasks:
            task.cancel()  # cancellation is the consumer's shutdown signal
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()

    app = FastAPI(title="analytics", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    mount_observability(app, engine=engine)
    app.state.service = AnalyticsService(sessions)
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "analytics"}

    return app


app = create_app()
