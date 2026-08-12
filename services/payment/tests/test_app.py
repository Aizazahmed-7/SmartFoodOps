"""App wiring + the append-only ledger guarantee (a source-scan test)."""

import asyncio
import pathlib

from fastapi.testclient import TestClient
from payment.config import Settings
from payment.main import create_app


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "payment"}


def test_ledger_is_append_only_by_source_scan():
    """No UPDATE or DELETE may ever target the ledger table. History is
    corrected by reversing entries, never edited — grep-enforced."""
    package = pathlib.Path(__file__).parent.parent / "payment"
    offenders = []
    for path in package.rglob("*.py"):
        source = path.read_text()
        if "ledger.update" in source or "ledger.delete" in source:
            offenders.append(path.name)
    assert offenders == []


def test_app_builds_real_gateway_when_none_injected():
    app = create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
    # lifespan shutdown closed the app-owned http client


def test_injected_poller_lives_and_dies_with_the_app():
    class StubPoller:
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

    poller = StubPoller()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True),
        poller=poller,  # type: ignore[arg-type]
    )
    with TestClient(app):
        pass
    assert poller.started and poller.cancelled
