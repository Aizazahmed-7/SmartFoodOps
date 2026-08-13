"""App wiring: healthz, and the consumer task living and dying with the app."""

import asyncio

from fastapi.testclient import TestClient
from notification.config import Settings
from notification.main import create_app


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "notification"}


def test_lifespan_runs_and_cancels_both_injected_consumers():
    """Same contract as every other background task in the fleet: BOTH
    consumer tasks (orders + payments loops) live and die with the app."""

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

    orders, payments = StubRunner(), StubRunner()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True),
        consumers=[orders, payments],  # type: ignore[list-item]
    )
    with TestClient(app):
        pass  # lifespan enters and exits
    assert orders.started and orders.cancelled
    assert payments.started and payments.cancelled
