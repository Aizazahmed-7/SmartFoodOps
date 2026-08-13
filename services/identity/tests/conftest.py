import pytest
from fastapi.testclient import TestClient
from identity.config import Settings
from identity.main import create_app


@pytest.fixture()
def settings(tmp_path):
    """The canonical app Settings for tests: in-memory sqlite, a throwaway
    signing key, create_all on lifespan."""
    return Settings(
        database_url="sqlite+aiosqlite://",
        create_all=True,
        signing_key_path=str(tmp_path / "key.pem"),
        token_issuer="http://identity.test",
    )


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c
