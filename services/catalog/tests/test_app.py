def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "catalog"}


def test_error_envelope_installed(client):
    body = client.get("/no-such-route").json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body["error"]


def test_app_without_di_owns_its_grants_client():
    """No injected port → the real adapter + an owned httpx client that the
    lifespan must close on shutdown (nothing is ever sent in this test)."""
    from catalog.config import Settings
    from catalog.main import create_app
    from fastapi.testclient import TestClient

    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    with TestClient(create_app(settings)):
        pass


def test_lifespan_runs_and_cancels_injected_poller(grants, cache, search_port):
    """The drain task starts with the app and dies with it."""
    import asyncio

    from catalog.config import Settings
    from catalog.main import create_app
    from fastapi.testclient import TestClient

    class FakePoller:
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

    fake = FakePoller()
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(
        settings,
        grants=grants,
        cache=cache,
        search=search_port,
        poller=fake,  # type: ignore[arg-type]
    )
    with TestClient(app):
        pass
    assert fake.started and fake.cancelled
