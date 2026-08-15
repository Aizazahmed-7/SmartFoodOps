import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from order.config import Settings
from order.domain.ports import AddressNotFound
from order.domain.transitions import transition
from order.main import create_app
from smartfood_auth import AuthContext, headers_for
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))


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
        self.cancels: list[str] = []
        self.fail_with: Exception | None = None

    async def start(self, order_id: str) -> bool:
        self.started.append(order_id)
        return True

    async def signal_decision(self, order_id: str, verdict: str) -> None:
        self._maybe_fail()
        self.decisions.append((order_id, verdict))

    async def signal_food_ready(self, order_id: str) -> None:
        self._maybe_fail()
        self.food_ready.append(order_id)

    async def signal_cancel(self, order_id: str) -> None:
        self._maybe_fail()
        self.cancels.append(order_id)

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
    settings = Settings(
        database_url="sqlite+aiosqlite://", create_all=True, sweeper_interval_seconds=0
    )
    app = create_app(settings, catalog=catalog, identity=identity, saga=saga)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db_url(tmp_path):
    # FILE-backed so a test's seeding engine and the app share one DB.
    return f"sqlite+aiosqlite:///{tmp_path}/order.db"


# ── shared builders ────────────────────────────────────────────────
# importlib test mode means test modules cannot import one another (or
# this file); conftest FIXTURES are the sanctioned sharing channel, so
# the builders below travel as fixtures returning callables.


def _default_items():
    return [
        {
            "id": "itm_a",
            "name": "Chicken Biryani",
            "price_cents": 1200,
            "currency": "USD",
            "available": True,
            "modifier_groups": [],
        }
    ]


@pytest.fixture()
def make_snapshot():
    """Builder for the canonical catalog snapshot (Biryani House / itm_a);
    pass `items` when a suite needs richer menu contents."""

    def _snapshot(*, version=3, status="open", items=None):
        return {
            "restaurant": {
                "id": "rst_1",
                "name": "Biryani House",
                "city": "springfield",
                "status": status,
                "version": version,
            },
            "items": _default_items() if items is None else items,
            "missing_item_ids": [],
        }

    return _snapshot


@pytest.fixture()
def make_order_body():
    """Builder for the canonical placement payload; keyword overrides
    replace whole fields (`lines=...` wins over `qty`)."""

    def _body(*, menu_version=3, qty=2, **overrides):
        payload = {
            "restaurant_id": "rst_1",
            "menu_version": menu_version,
            "address_id": "adr_1",
            "card_token": "tok_ok",
            "lines": [{"item_id": "itm_a", "qty": qty}],
        }
        payload.update(overrides)
        return payload

    return _body


@pytest.fixture()
def place_order(make_order_body):
    """POST a valid order (as usr_1 unless `headers` says otherwise) and
    return its order_id."""

    def _place(client, *, qty=1, headers=None):
        r = client.post(
            "/v1/orders",
            json=make_order_body(qty=qty),
            headers={**(headers or _CUSTOMER), "Idempotency-Key": uuid.uuid4().hex},
        )
        assert r.status_code == 202
        return r.json()["order_id"]

    return _place


@pytest.fixture()
def advance_order():
    """Walk an order through legal guarded transitions (the same writer
    the saga uses) against the shared FILE-backed db; `reason` lands on
    the final move."""

    def _advance(db_url, order_id, moves, *, reason=None):
        async def _go():
            engine = create_async_engine(db_url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            for index, (expected, target) in enumerate(moves):
                final = index == len(moves) - 1
                await transition(
                    sessions,
                    order_id,
                    expected=expected,
                    target=target,
                    cancel_reason=reason if final else None,
                )
            await engine.dispose()

        asyncio.run(_go())

    return _advance
