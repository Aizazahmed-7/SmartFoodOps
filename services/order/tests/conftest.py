import pytest
from fastapi.testclient import TestClient
from order.config import Settings
from order.domain.ports import AddressNotFound
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


class FakeIdentity:
    """In-memory IdentityPort: {(user_id, address_id): address_dict}."""

    def __init__(self):
        self.addresses: dict[tuple[str, str], dict] = {}
        self.fail_with: Exception | None = None

    async def get_address(self, user_id: str, address_id: str) -> dict:
        if self.fail_with is not None:
            raise self.fail_with
        address = self.addresses.get((user_id, address_id))
        if address is None:
            raise AddressNotFound(address_id)
        return address


class RecordingSaga:
    """In-memory SagaPort: records starts and signals; `fail_with` makes the
    NEXT signal raise (SagaGone / SagaUnavailable injection), then clears."""

    def __init__(self):
        self.started: list[str] = []
        self.decisions: list[tuple[str, str]] = []
        self.food_ready: list[str] = []
        self.fail_with: Exception | None = None

    async def start(self, order_id: str) -> None:
        self.started.append(order_id)

    async def signal_decision(self, order_id: str, verdict: str) -> None:
        self._maybe_fail()
        self.decisions.append((order_id, verdict))

    async def signal_food_ready(self, order_id: str) -> None:
        self._maybe_fail()
        self.food_ready.append(order_id)

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc


@pytest.fixture()
def catalog():
    return FakeCatalog()


@pytest.fixture()
def identity():
    identity = FakeIdentity()
    identity.addresses[("usr_1", "adr_1")] = {
        "id": "adr_1",
        "label": "home",
        "line1": "12 Mango St",
        "city": "Springfield",
        "lat": None,
        "lon": None,
    }
    return identity


@pytest.fixture()
def saga():
    return RecordingSaga()


@pytest.fixture()
def client(catalog, identity, saga):
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    app = create_app(settings, catalog=catalog, identity=identity, saga=saga)
    with TestClient(app) as c:
        yield c
