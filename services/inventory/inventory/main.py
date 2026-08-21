"""Inventory service — stock counters, capacity slots, and the reservation
ledger the order saga reserves against (docs/service-ownership.md)."""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_kafka import AvroSerde, EventConsumer, EventProducer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from smartfood_outbox import OutboxPoller
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .api.routes import router
from .config import Settings
from .db import metadata, outbox
from .domain.service import InventoryService
from .reaper import Reaper


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
    poller: OutboxPoller | None = None,
    consumer: EventConsumer | None = None,
    reaper: Reaper | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("inventory")
    setup_tracing("inventory", settings.otlp_endpoint)

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    service = InventoryService(
        sessions,
        default_capacity=settings.default_capacity,
        reservation_ttl_seconds=settings.reservation_ttl_seconds,
    )
    if reaper is None:
        reaper = Reaper(service, interval_seconds=settings.reaper_interval_seconds)

    events_topic = topic(settings.cell_id, Topic.INVENTORY_EVENTS)
    own_producer: EventProducer | None = None
    if poller is None and settings.outbox_mode == "poller":  # pragma: no cover — live wiring
        own_producer = EventProducer(
            settings.kafka_bootstrap, AvroSerde(SchemaRegistry(settings.schema_registry_url))
        )
        poller = OutboxPoller(
            sessions, outbox, topic=events_topic, producer=own_producer, cell_id=settings.cell_id
        )

    live_consumer = consumer
    if live_consumer is None and settings.kafka_consumers == "on":  # pragma: no cover — live
        # Live wiring only: the loop, its aiokafka config, and the
        # retry/DLQ policy all live (tested) in smartfood-kafka.
        from .consumers import GROUP, StockProvisioningHandler

        live_consumer = EventConsumer(
            topic(settings.cell_id, Topic.CATALOG_CHANGES),
            GROUP,
            StockProvisioningHandler(sessions, default_capacity=settings.default_capacity),
            AvroSerde(SchemaRegistry(settings.schema_registry_url)),
            bootstrap=settings.kafka_bootstrap,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        tasks: list[asyncio.Task[None]] = []
        if poller is not None:
            if own_producer is not None:  # pragma: no cover — live path
                await own_producer.start()
            tasks.append(asyncio.create_task(poller.run()))
        if live_consumer is not None:
            tasks.append(asyncio.create_task(live_consumer.run()))
        tasks.append(asyncio.create_task(reaper.run()))
        yield
        for task in tasks:
            task.cancel()  # cancellation IS the shutdown signal
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        if own_producer is not None:  # pragma: no cover — live path
            await own_producer.stop()
        await engine.dispose()

    app = FastAPI(title="inventory", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    mount_observability(app, engine=engine)
    app.state.service = service
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "inventory"}

    return app


app = create_app()
