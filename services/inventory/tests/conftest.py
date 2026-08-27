import pytest
from fastapi.testclient import TestClient
from inventory.config import Settings
from inventory.main import create_app


class _NoParents:
    """Catalog 'reachable', every row parentless: keeps the pre-brands
    foreign-restaurant semantics (mismatch → 404) for the default suite.
    The brand/503 behaviors are exercised with real fakes in
    test_stock_composite.py."""

    async def brand_of(self, restaurant_id: str) -> str | None:
        return None


@pytest.fixture()
def client():
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, parents=_NoParents())  # type: ignore[arg-type]
    with TestClient(app) as c:  # `with` runs the lifespan (create_all + reaper task)
        yield c
