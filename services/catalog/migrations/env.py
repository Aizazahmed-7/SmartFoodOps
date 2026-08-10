"""Alembic environment (async engine variant).

Invoked by `alembic upgrade` — in dev, programmatically at service startup
(catalog.main._run_migrations), in a thread so it may own its own event loop.
"""

import asyncio

from alembic import context
from catalog.db import metadata
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
target_metadata = metadata


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("sqlalchemy.url not configured")
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations not supported")

asyncio.run(_run_async())
