"""Payment service — ledger + money idempotency around the PaymentGateway
port (ADR-0010). Only this service ever imports a PSP adapter."""

import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from smartfood_api import install_error_handlers
from smartfood_idempotency import IdempotencyStore
from smartfood_kafka import AvroSerde, EventProducer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging
from smartfood_outbox import OutboxPoller
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .adapters.psp import MockPspClient
from .api.routes import router
from .config import Settings
from .db import idempotency_keys, metadata, outbox
from .domain.ports import PaymentGatewayPort
from .domain.service import PaymentService


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
    gateway: PaymentGatewayPort | None = None,
    poller: OutboxPoller | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("payment")

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    own_http: httpx.AsyncClient | None = None
    if gateway is None:
        # Timeout is the adapter's clock: mock-psp's tok_timeout hangs 30s,
        # so 5s here is what turns it into the ambiguous-outcome case.
        own_http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.internal_timeout_seconds, connect=settings.internal_connect_timeout_seconds
            )
        )
        gateway = MockPspClient(settings.mock_psp_base_url, own_http)

    events_topic = topic(settings.cell_id, Topic.PAYMENTS_EVENTS)
    own_producer: EventProducer | None = None
    if poller is None and settings.outbox_mode == "poller":  # pragma: no cover — live wiring
        own_producer = EventProducer(
            settings.kafka_bootstrap, AvroSerde(SchemaRegistry(settings.schema_registry_url))
        )
        poller = OutboxPoller(
            sessions, outbox, topic=events_topic, producer=own_producer, cell_id=settings.cell_id
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        drain_task: asyncio.Task[None] | None = None
        if poller is not None:
            if own_producer is not None:  # pragma: no cover — live path
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

    app = FastAPI(title="payment", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.state.service = PaymentService(
        sessions, gateway, IdempotencyStore(sessions, idempotency_keys)
    )
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "payment"}

    return app


app = create_app()
