"""App wiring: healthz, lifespan-owned runner tasks, migrations gate."""

import asyncio

from analytics.config import Settings
from analytics.main import create_app
from fastapi.testclient import TestClient


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "analytics"}


def test_injected_runner_lives_and_dies_with_the_app():
    """The consumer task is lifespan-owned: started on boot, cancelled on
    shutdown — the same contract as notification's loops and the outbox
    poller."""
    state = {"started": False, "cancelled": False}

    async def runner():
        state["started"] = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True), runners=[runner]
    )
    with TestClient(app):
        pass
    assert state["started"] and state["cancelled"]


def test_metrics_endpoint_is_mounted(client):
    assert client.get("/metrics").status_code == 200
