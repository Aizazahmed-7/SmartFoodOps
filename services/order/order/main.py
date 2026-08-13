"""Order service — the state-machine owner (docs/service-ownership.md).

S3 scope: quote + idempotent placement + reads, outbox → c1.orders.events.
The saga port is a logging stub until S5 wires Temporal. Flag #7: only THIS
process runs the OutboxPoller (single-instance ordering); the worker
process only stages rows.
"""

import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from smartfood_api import install_error_handlers
from smartfood_idempotency import IdempotencyStore
from smartfood_kafka import AvroSerde, EventProducer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging
from smartfood_outbox import OutboxPoller
from smartfood_pricing import PricingConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .adapters.catalog_client import CatalogClient
from .adapters.identity_client import IdentityClient
from .adapters.temporal_client import TemporalSaga
from .api.restaurant import router as restaurant_router
from .api.routes import router
from .config import Settings
from .db import idempotency_keys, metadata, outbox
from .domain.kitchen import KitchenService
from .domain.ports import CatalogPort, IdentityPort, SagaPort
from .domain.service import OrderService


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
    catalog: CatalogPort | None = None,
    identity: IdentityPort | None = None,
    saga: SagaPort | None = None,
    poller: OutboxPoller | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("order")

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # DI seams: tests inject fakes; production builds the real adapters and
    # owns their clients' lifecycles in the lifespan below.
    own_http: httpx.AsyncClient | None = None
    if catalog is None or identity is None:
        own_http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0))
        catalog = catalog or CatalogClient(settings.catalog_base_url, own_http)
        identity = identity or IdentityClient(settings.identity_base_url, own_http)
    if saga is None:
        # Lazy-connecting: no Temporal traffic until the first placement.
        saga = TemporalSaga(
            settings.temporal_address,
            task_queue=settings.task_queue,
            accept_timeout_s=settings.accept_timeout_s,
            pickup_delay_s=settings.pickup_delay_s,
            dropoff_delay_s=settings.dropoff_delay_s,
        )

    events_topic = topic(settings.cell_id, Topic.ORDERS_EVENTS)
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

    app = FastAPI(title="order", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.state.service = OrderService(
        catalog,
        pricing=PricingConfig(
            delivery_fee_cents=settings.delivery_fee_cents,
            tax_basis_points=settings.tax_basis_points,
        ),
        sessions=sessions,
        identity=identity,
        saga=saga,
        idempotency=IdempotencyStore(sessions, idempotency_keys),
    )
    app.state.kitchen = KitchenService(sessions, saga=saga)
    app.include_router(router)
    app.include_router(restaurant_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "order"}

    return app


app = create_app()
