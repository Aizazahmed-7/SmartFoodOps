"""Identity service — issues the tokens the rest of the platform trusts."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from smartfood_api import install_error_handlers
from smartfood_auth import TokenIssuer
from smartfood_otel import RequestContextMiddleware, setup_logging
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .api.routes import router
from .config import Settings
from .db import metadata
from .domain.service import IdentityService
from .keys import load_or_generate


def _run_migrations(database_url: str) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")


def create_app(settings: Settings | None = None) -> FastAPI:
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
            await asyncio.to_thread(_run_migrations, settings.database_url)
        yield
        await engine.dispose()

    app = FastAPI(title="identity", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

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
    app.state.service = IdentityService(sessions, issuer, settings)

    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "identity"}

    return app


app = create_app()
