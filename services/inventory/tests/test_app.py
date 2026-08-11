"""App wiring: healthz, DI seams, lifespan task management."""

import asyncio

from fastapi.testclient import TestClient
from inventory.config import Settings
from inventory.main import create_app


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "inventory"}


def test_lifespan_runs_and_cancels_injected_tasks():
    """Injected poller/consumer/reaper live and die with the app — the same
    contract as catalog's poller and identity's consumer."""

    class StubRunner:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def run(self):
            self.started = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    poller, consumer, reaper = StubRunner(), StubRunner(), StubRunner()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True),
        poller=poller,  # type: ignore[arg-type]
        consumer=consumer,  # type: ignore[arg-type]
        reaper=reaper,  # type: ignore[arg-type]
    )
    with TestClient(app):
        pass  # lifespan enters and exits
    assert poller.started and poller.cancelled
    assert consumer.started and consumer.cancelled
    assert reaper.started and reaper.cancelled
