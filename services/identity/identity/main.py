"""Identity service — issues the tokens the rest of the platform trusts."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from smartfood_auth import TokenIssuer
from smartfood_otel import RequestContextMiddleware, setup_logging
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from .config import Settings
from .db import metadata
from .keys import load_or_generate
from .routes import router


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

    key = load_or_generate(settings.signing_key_path)
    app.state.settings = settings
    app.state.key = key
    app.state.issuer = TokenIssuer(
        key,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        ttl_seconds=settings.access_ttl_seconds,
    )
    app.state.sessions = async_sessionmaker(engine, expire_on_commit=False)

    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "identity"}

    return app


app = create_app()
