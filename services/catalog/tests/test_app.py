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
