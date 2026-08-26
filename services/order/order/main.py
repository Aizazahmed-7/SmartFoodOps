"""Order service — the state-machine owner (docs/service-ownership.md).

Scope: quote + idempotent placement + reads, outbox → c1.orders.events.
Flag #7: only THIS process runs the OutboxPoller (single-instance
ordering); the worker process only stages rows — including, since
ADR-0023, the OrderPlaced row its create_order activity writes.
"""

import asyncio
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_kafka import AvroSerde, EventProducer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from smartfood_outbox import OutboxPoller
from smartfood_pricing import PricingConfig
from smartfood_realtime import RedisRealtime, StreamConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from . import tracking
from .adapters.catalog_client import CatalogClient
from .adapters.identity_client import IdentityClient
from .adapters.temporal_client import TemporalSaga
from .api.restaurant import router as restaurant_router
from .api.routes import router
from .api.track import TrackingPort
from .api.track import router as track_router
from .config import Settings
from .db import metadata, outbox
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
    tracking_port: TrackingPort | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("order")
    setup_tracing("order", settings.otlp_endpoint)

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # Live tracking (S4): the ticket store + status bus. Armed only with a
    # redis_url; otherwise the FE's polling loop remains the whole story.
    own_tracking: RedisRealtime | None = None
    if tracking_port is None and settings.redis_url:  # pragma: no cover — live wiring
        import redis.asyncio as aioredis

        own_tracking = RedisRealtime(aioredis.from_url(settings.redis_url))
        tracking_port = own_tracking
    if tracking_port is not None:
        tracking.set_publisher(tracking_port)  # type: ignore[arg-type]

    # DI seams: tests inject fakes; production builds the real adapters and
    # owns their clients' lifecycles in the lifespan below.
    own_http: httpx.AsyncClient | None = None
    if catalog is None or identity is None:
        own_http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.internal_timeout_seconds, connect=settings.internal_connect_timeout_seconds
            )
        )
        catalog = catalog or CatalogClient(settings.catalog_base_url, own_http)
        identity = identity or IdentityClient(settings.identity_base_url, own_http)
    if saga is None:
        # Lazy-connecting: no Temporal traffic until the first placement.
        saga = TemporalSaga(
            settings.temporal_address,
            task_queue=settings.task_queue,
            accept_timeout_s=settings.accept_timeout_s,
            forward_deadline_s=settings.forward_deadline_s,
            dispatch_knobs={
                "offer_first_timeout_s": settings.offer_first_timeout_s,
                "offer_next_timeout_s": settings.offer_next_timeout_s,
                "no_rider_deadline_s": settings.no_rider_deadline_s,
                "no_candidates_retry_s": settings.no_candidates_retry_s,
                "pickup_timeout_s": settings.pickup_timeout_s,
            },
            await_seconds=settings.placement_await_seconds,
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
        tasks: list[asyncio.Task[None]] = []
        if poller is not None:
            if own_producer is not None:  # pragma: no cover — live path
                await own_producer.start()
            tasks.append(asyncio.create_task(poller.run()))
        yield
        for task in tasks:
            task.cancel()  # cancellation IS the tasks' shutdown signal
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        if own_producer is not None:  # pragma: no cover — live path
            await own_producer.stop()
        await engine.dispose()
        tracking.reset_publisher()
        if own_tracking is not None:  # pragma: no cover — live wiring
            await own_tracking.aclose()
        if own_http is not None:
            await own_http.aclose()

    app = FastAPI(title="order", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    mount_observability(app, engine=engine)
    # Exposed for the same reason state.service is: it is this app's one
    # database handle. Tests bind their in-process saga double to it so
    # placement writes land in the app's own (in-memory) database.
    app.state.sessions = sessions
    app.state.service = OrderService(
        catalog,
        pricing=PricingConfig(
            delivery_fee_cents=settings.delivery_fee_cents,
            tax_basis_points=settings.tax_basis_points,
        ),
        sessions=sessions,
        identity=identity,
        saga=saga,
    )
    app.state.kitchen = KitchenService(sessions, saga=saga)
    # The internal courier relay (dispatch → dlv:: signals) speaks to the
    # saga port directly — a translation, not a domain decision.
    app.state.saga = saga
    app.state.tracking = tracking_port
    app.state.tracking_config = StreamConfig(
        ticket_ttl_s=settings.track_ticket_ttl_seconds,
        heartbeat_s=settings.track_heartbeat_seconds,
        lifetime_min_s=settings.track_lifetime_min_seconds,
        lifetime_max_s=settings.track_lifetime_max_seconds,
    )
    app.include_router(router)
    app.include_router(restaurant_router)
    app.include_router(track_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "order"}

    return app


app = create_app()
