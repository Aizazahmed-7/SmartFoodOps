"""Notification service — the durable inbox over the order/payment topics
(docs/service-ownership.md). Consumer-only: no outbox, no poller."""

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_kafka import AvroSerde, EventConsumer, SchemaRegistry, Topic, topic
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing
from smartfood_realtime import RedisRealtime, StreamConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from . import push, receipt_queue
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
    settings: Settings | None = None,
    *,
    consumers: Sequence[EventConsumer] | None = None,
    realtime: "RedisRealtime | None" = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("notification")
    setup_tracing("notification", settings.otlp_endpoint)

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # Bell push (S9): armed only with a redis_url — otherwise the FE's
    # 15s poll remains the whole story, by design.
    own_realtime: RedisRealtime | None = None
    if realtime is None and settings.redis_url:  # pragma: no cover — live wiring
        import redis.asyncio as aioredis

        own_realtime = RedisRealtime(aioredis.from_url(settings.redis_url))
        realtime = own_realtime
    if realtime is not None:
        push.set_publisher(realtime)  # type: ignore[arg-type]

    # Receipts (S10): a configured broker arms the post-commit nudge. The
    # import is deferred so test apps (and any deployment that never sets
    # a broker) don't pay for celery/boto3 at startup.
    if settings.celery_broker_url:  # pragma: no cover — live wiring
        from .tasks import enqueue_receipt_chain

        receipt_queue.set_queue(enqueue_receipt_chain)

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
        push.reset_publisher()
        receipt_queue.reset_queue()
        if own_realtime is not None:  # pragma: no cover — live wiring
            await own_realtime.aclose()

    app = FastAPI(title="notification", lifespan=lifespan)
    # SSE lanes are lifetimes, not latencies — keep them out of the
    # p95 histogram and out of Jaeger (smartfood-otel stream_prefixes).
    app.add_middleware(RequestContextMiddleware, stream_prefixes=("/v1/notifications/stream",))
    install_error_handlers(app)
    mount_observability(app, engine=engine)
    app.state.realtime = realtime
    app.state.stream_config = StreamConfig(
        ticket_ttl_s=settings.stream_ticket_ttl_seconds,
        heartbeat_s=settings.stream_heartbeat_seconds,
        lifetime_min_s=settings.stream_lifetime_min_seconds,
        lifetime_max_s=settings.stream_lifetime_max_seconds,
    )
    app.state.service = NotificationService(sessions)
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "notification"}

    return app


app = create_app()
