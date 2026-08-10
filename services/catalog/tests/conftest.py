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


class FakeCache:
    """In-memory CachePort. Records every op (kind, key) so tests can assert
    exact cache behavior — e.g. blob-then-pointer write order."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.locks: set[str] = set()
        self.ops: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        self.ops.append(("get", key))
        return self.data.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self.ops.append(("set", key))
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.ops.append(("delete", key))
        self.data.pop(key, None)

    async def acquire_lock(self, key: str, ttl_ms: int) -> bool:
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    async def release_lock(self, key: str) -> None:
        self.locks.discard(key)


class DownCache:
    """CachePort when Redis is unreachable, per the port contract: every
    read misses, every write vanishes, locks always 'acquire'."""

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        pass

    async def delete(self, key: str) -> None:
        pass

    async def acquire_lock(self, key: str, ttl_ms: int) -> bool:
        return True

    async def release_lock(self, key: str) -> None:
        pass


class FakeSearch:
    """In-memory SearchPort: returns pre-set hits, records call kwargs."""

    def __init__(self):
        self.hits: list[dict] = []
        self.calls: list[dict] = []

    async def search(self, **kwargs) -> list[dict]:
        self.calls.append(kwargs)
        return self.hits


@pytest.fixture()
def grants():
    return FakeGrants()


@pytest.fixture()
def search_port():
    return FakeSearch()


@pytest.fixture()
def cache():
    return FakeCache()


@pytest.fixture()
def client(grants, cache, search_port):
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, grants=grants, cache=cache, search=search_port)
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c


@pytest.fixture()
def down_client(grants):
    """A full app wired to an unreachable cache — the degraded mode."""
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, grants=grants, cache=DownCache())
    with TestClient(app) as c:
        yield c
