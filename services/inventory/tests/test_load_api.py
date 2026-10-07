"""FR-85: the kitchen-congestion read, and the honesty of its 404.

`active`/`capacity` gates reservations (FR-15) and was never published
anywhere. The explanation engine needs it to tell a saturated kitchen from
an idle one — and needs to know when it is being told nothing at all.
"""

import pytest
from inventory.adapters.repo import InventoryRepo
from inventory.db import metadata
from inventory.domain.service import InventoryService
from smartfood_auth import AuthContext, headers_for
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

SYSTEM = headers_for(AuthContext(sub="svc:ai-assistant", roles=frozenset({"system"})))
CUSTOMER = headers_for(AuthContext(sub="usr_1", roles=frozenset({"customer"})))
OWNER = headers_for(
    AuthContext(sub="usr_owner", roles=frozenset({"restaurant_admin"}), restaurant_id="rst_1")
)


async def _service():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return InventoryService(sessions), sessions


async def test_a_kitchen_with_no_load_row_is_unknown_not_idle():
    """The distinction the whole endpoint turns on. Rounding a missing row
    to `active: 0` would hand the resolver a fact nobody observed, and the
    engine would tell a customer the kitchen is quiet on the strength of
    an absent record."""
    svc, _ = await _service()
    assert await svc.load("rst_never_seen") is None


async def test_load_reports_what_the_capacity_gate_sees():
    svc, sessions = await _service()
    async with sessions() as s:
        repo = InventoryRepo(s)
        await repo.insert_load("rst_1", 8)
        for _ in range(7):
            assert await repo.occupy_slot("rst_1")
        await s.commit()
    assert await svc.load("rst_1") == (7, 8)


async def test_load_can_report_active_above_capacity():
    """Lowering capacity below current active is legal by design (the table
    deliberately has no `active <= capacity` CHECK): new orders stop,
    running ones drain. The read must not clamp or reorder that — a kitchen
    holding 5 with capacity 2 is exactly the state the engine should be
    able to explain."""
    svc, sessions = await _service()
    async with sessions() as s:
        repo = InventoryRepo(s)
        await repo.insert_load("rst_1", 5)
        for _ in range(5):
            await repo.occupy_slot("rst_1")
        await repo.set_capacity("rst_1", 2)
        await s.commit()
    assert await svc.load("rst_1") == (5, 2)


def test_the_endpoint_returns_the_facts_and_its_own_clock(client):
    client.put("/v1/inventory/restaurants/rst_1/capacity", json={"capacity": 6}, headers=OWNER)
    r = client.get("/v1/internal/restaurants/rst_1/load", headers=SYSTEM)
    assert r.status_code == 200
    body = r.json()
    assert body["restaurant_id"] == "rst_1"
    assert body["active"] == 0
    assert body["capacity"] == 6
    # The caller writes prose about "right now" and must be able to tell
    # how old "now" is.
    assert body["as_of"]


def test_an_unknown_kitchen_is_a_404_not_a_zero(client):
    r = client.get("/v1/internal/restaurants/rst_ghost/load", headers=SYSTEM)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("headers", [CUSTOMER, OWNER, {}])
def test_load_is_system_only(client, headers):
    """Congestion is an operational fact about a business, not something a
    customer or a rival's owner token gets to read."""
    r = client.get("/v1/internal/restaurants/rst_1/load", headers=headers)
    assert r.status_code in (401, 403)
