"""Identity service — issues the tokens the rest of the platform trusts."""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_auth import TokenIssuer
from smartfood_kafka import EventConsumer
from smartfood_otel import RequestContextMiddleware, setup_logging
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .api.routes import router
from .config import Settings
from .db import metadata, seed_roles
from .domain.service import IdentityService
from .keys import load_or_generate


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
    settings: Settings | None = None, *, consumer: EventConsumer | None = None
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("identity")

    engine_kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        engine_kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(settings.database_url, **engine_kwargs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.create_all:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            # Alembic manages its own (sync) connection; run it off the event loop.
            await asyncio.to_thread(_run_migrations, settings.database_url)  # pragma: no cover
        # Every boot, both paths: the roles lookup converges to the Role
        # enum (ADR-0022) — a new member can never be forgotten in a deploy.
        await seed_roles(engine)
        consumer_task: asyncio.Task[None] | None = None
        live_consumer = consumer
        if live_consumer is None and settings.kafka_consumers == "on":  # pragma: no cover
            # Live wiring only: the loop, its aiokafka config, and the
            # retry/DLQ policy all live (tested) in smartfood-kafka.
            from smartfood_kafka import AvroSerde, SchemaRegistry, Topic, topic

            from .consumers import GROUP, GrantConvergenceHandler

            live_consumer = EventConsumer(
                topic(settings.cell_id, Topic.CATALOG_CHANGES),
                GROUP,
                GrantConvergenceHandler(sessions, app.state.service),
                AvroSerde(SchemaRegistry(settings.schema_registry_url)),
                bootstrap=settings.kafka_bootstrap,
            )
        if live_consumer is not None:
            consumer_task = asyncio.create_task(live_consumer.run())
        yield
        if consumer_task is not None:
            consumer_task.cancel()  # cancellation is the consumer's shutdown signal
            with suppress(asyncio.CancelledError):
                await consumer_task
        await engine.dispose()

    app = FastAPI(title="identity", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    mount_observability(app, engine=engine)

    key = load_or_generate(settings.signing_key_path)
    issuer = TokenIssuer(
        key,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        ttl_seconds=settings.access_ttl_seconds,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # Composition root: repo ← service ← routes. The API layer only ever
    # sees app.state.service; the domain only sees the sessionmaker.
    app.state.key = key
    app.state.service = IdentityService(
        sessions,
        issuer,
        access_ttl_seconds=settings.access_ttl_seconds,
        refresh_ttl_days=settings.refresh_ttl_days,
    )

    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "identity"}

    return app


app = create_app()
