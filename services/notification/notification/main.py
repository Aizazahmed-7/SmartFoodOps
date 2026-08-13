"""Notification service — the durable inbox over the order/payment topics
(docs/service-ownership.md). Consumer-only: no outbox, no poller."""

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from smartfood_api import install_error_handlers
from smartfood_kafka import AvroSerde, EventConsumer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .api.routes import router
from .config import Settings
from .db import metadata
from .domain.service import NotificationService


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
    settings: Settings | None = None, *, consumers: Sequence[EventConsumer] | None = None
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("notification")

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    live_consumers = list(consumers) if consumers is not None else []
    if not live_consumers and settings.kafka_consumers == "on":  # pragma: no cover — live
        # Live wiring only: the loop, its aiokafka config, and the
        # retry/DLQ policy all live (tested) in smartfood-kafka. One
        # consumer PER topic, separate groups: a payments-side
        # ProjectionLag backoff must never block the orders loop that
        # arms the projection it is waiting for (see consumers.py).
        from .consumers import GROUP_ORDERS, GROUP_PAYMENTS, InboxHandler

        handler = InboxHandler(sessions)
        serde = AvroSerde(SchemaRegistry(settings.schema_registry_url))
        live_consumers = [
            EventConsumer(
                topic(settings.cell_id, Topic.ORDERS_EVENTS),
                GROUP_ORDERS,
                handler,
                serde,
                bootstrap=settings.kafka_bootstrap,
            ),
            EventConsumer(
                topic(settings.cell_id, Topic.PAYMENTS_EVENTS),
                GROUP_PAYMENTS,
                handler,
                serde,
                bootstrap=settings.kafka_bootstrap,
            ),
        ]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        tasks = [asyncio.create_task(consumer.run()) for consumer in live_consumers]
        yield
        for task in tasks:
            task.cancel()  # cancellation is the consumers' shutdown signal
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()

    app = FastAPI(title="notification", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.state.service = NotificationService(sessions)
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "notification"}

    return app


app = create_app()
