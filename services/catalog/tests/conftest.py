import pytest
from catalog.config import Settings
from catalog.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings)
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c
