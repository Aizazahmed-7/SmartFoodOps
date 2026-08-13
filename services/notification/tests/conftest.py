import pytest
from fastapi.testclient import TestClient
from notification.config import Settings
from notification.main import create_app


@pytest.fixture()
def app():
    return create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))


@pytest.fixture()
def client(app):
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c
