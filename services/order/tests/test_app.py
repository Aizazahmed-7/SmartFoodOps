"""App wiring: healthz, real-adapter construction, lifespan cleanup."""

from fastapi.testclient import TestClient
from order.config import Settings
from order.main import create_app


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "order"}


def test_app_builds_real_catalog_client_when_none_injected():
    """No catalog injected → the app constructs the real adapter and owns
    its http client's lifecycle (closed on shutdown without error)."""
    app = create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
    # exiting the context ran the lifespan shutdown — own_http.aclose()


def test_worker_skeleton_importable():
    """The keep-alive skeleton's only contract until S5 lands the real
    Temporal worker: importable, with a callable main."""
    from order import worker

    assert callable(worker.main)


async def test_saga_stub_logs_and_returns():
    from order.adapters.saga_stub import SagaNotYetWired

    await SagaNotYetWired().start("ord_x")  # observable no-op until S5


def test_injected_poller_lives_and_dies_with_the_app():
    import asyncio

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
