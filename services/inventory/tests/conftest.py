import pytest
from fastapi.testclient import TestClient
from inventory.config import Settings
from inventory.main import create_app


@pytest.fixture()
def client():
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings)
    with TestClient(app) as c:  # `with` runs the lifespan (create_all + reaper task)
        yield c
