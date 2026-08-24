import pytest
from analytics.config import Settings
from analytics.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture()
def app():
    return create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))


@pytest.fixture()
def client(app):
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c
