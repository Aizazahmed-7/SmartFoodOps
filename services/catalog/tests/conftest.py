import pytest
from catalog.config import Settings
from catalog.main import create_app
from fastapi.testclient import TestClient


class FakeGrants:
    """In-memory GrantsPort: records calls; raises `fail_with` if set
    (recording first, so tests can assert the attempt happened)."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    async def grant_restaurant_admin(self, *, user_id: str, restaurant_id: str) -> None:
        self.calls.append((user_id, restaurant_id))
        if self.fail_with is not None:
            raise self.fail_with


@pytest.fixture()
def grants():
    return FakeGrants()


@pytest.fixture()
def client(grants):
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, grants=grants)
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c
