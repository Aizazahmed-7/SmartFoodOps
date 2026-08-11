import pytest
from fastapi.testclient import TestClient
from order.config import Settings
from order.main import create_app


class FakeCatalog:
    """In-memory CatalogPort: serves a canned snapshot, records calls,
    raises `fail_with` if set (recording first, so tests can assert the
    attempt happened)."""

    def __init__(self):
        self.snapshot: dict = {}
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_with: Exception | None = None

    async def get_snapshot(self, restaurant_id: str, item_ids: list[str]) -> dict:
        self.calls.append((restaurant_id, item_ids))
        if self.fail_with is not None:
            raise self.fail_with
        return self.snapshot


@pytest.fixture()
def catalog():
    return FakeCatalog()


@pytest.fixture()
def client(catalog):
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, catalog=catalog)
    with TestClient(app) as c:
        yield c
