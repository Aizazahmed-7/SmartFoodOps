"""Brands slice 3 (ADR-0028): the composite stock key — one fridge count per
(branch, item) — the parent-aware ownership check, and its catalog lookup."""

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from inventory.adapters.catalog_parent import CatalogParents, CatalogUnavailable
from inventory.adapters.repo import InventoryRepo
from inventory.config import Settings
from inventory.db import stock
from inventory.main import create_app
from smartfood_auth import AuthContext, headers_for

from .test_consumer import StockProvisioningHandler, _catalog_event, _service


def _admin(restaurant_id: str) -> dict[str, str]:
    return headers_for(
        AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=restaurant_id)
    )


# ── the composite key itself ───────────────────────────────────────


async def test_provisioning_fans_a_shared_base_item_into_every_branch():
    """Two branches' effective-menu events carry the SAME base item id: each
    must get its own zero-stock row — the exact silent-vanish the old
    item_id-only PK caused (the second insert conflicted away)."""
    _, sessions = await _service()
    handler = StockProvisioningHandler(sessions)
    await handler.handle(_catalog_event(restaurant_id="rst_dt", item_ids=("itm_base",)))
    await handler.handle(_catalog_event(restaurant_id="rst_ap", item_ids=("itm_base",)))
    async with sessions() as s:
        rows = (await s.execute(sa.select(stock).order_by(stock.c.restaurant_id))).all()
    assert [(r.restaurant_id, r.item_id, r.available) for r in rows] == [
        ("rst_ap", "itm_base", 0),
        ("rst_dt", "itm_base", 0),
    ]


async def test_branch_counts_move_independently():
    svc, _ = await _service()
    await svc.set_stock("rst_dt", "itm_base", 10)
    await svc.set_stock("rst_ap", "itm_base", 3)
    await svc.reserve(order_id="ord_1", restaurant_id="rst_dt", lines=_line("itm_base", 4))
    downtown = {r.item_id: r.available for r in await svc.list_stock("rst_dt")}
    airport = {r.item_id: r.available for r in await svc.list_stock("rst_ap")}
    assert downtown == {"itm_base": 6}  # decremented here…
    assert airport == {"itm_base": 3}  # …untouched there


def _line(item_id: str, qty: int):
    from inventory.domain.models import ReservationLine

    return [ReservationLine(item_id=item_id, qty=qty)]


async def test_set_stock_lost_insert_race_takes_the_update_path(monkeypatch):
    """Update saw nothing → insert conflicted (a concurrent writer beat us
    to THIS exact pair — the only collision the composite key allows) →
    the re-update wins. StockScopeMismatch is gone because the state it
    named is unrepresentable."""
    svc, sessions = await _service()
    real_insert = InventoryRepo.insert_stock

    async def racing_insert(self, rid, item_id, available, now):
        async with sessions() as other:  # the winner — a different tx entirely
            await real_insert(InventoryRepo(other), rid, item_id, 99, now)
            await other.commit()
        return await real_insert(self, rid, item_id, available, now)  # False

    monkeypatch.setattr(InventoryRepo, "insert_stock", racing_insert)
    row = await svc.set_stock("rst_1", "itm_a", 7)
    assert (row.available, row.version) == (7, 1)  # loser's UPDATE over the winner's 99


# ── the branch→brand lookup adapter ────────────────────────────────


def _parents(handler) -> CatalogParents:
    return CatalogParents(
        "http://catalog.test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_found_parentage_is_memoized_one_lookup_ever():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"id": "rst_dt", "brand_id": "brd_1"})

    parents = _parents(handler)
    assert await parents.brand_of("rst_dt") == "brd_1"
    assert await parents.brand_of("rst_dt") == "brd_1"
    assert calls["n"] == 1  # parentage is immutable — cached for the process


async def test_unknown_branch_is_none_and_never_memoized():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    parents = _parents(handler)
    assert await parents.brand_of("rst_new") is None
    assert await parents.brand_of("rst_new") is None
    assert calls["n"] == 2  # a branch created tomorrow must resolve tomorrow


async def test_catalog_trouble_raises_unavailable():
    with pytest.raises(CatalogUnavailable):
        await _parents(lambda _: httpx.Response(500)).brand_of("rst_dt")

    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(CatalogUnavailable):
        await _parents(refused).brand_of("rst_dt")


# ── the parent-aware _own over the API ─────────────────────────────


def _app_with(handler) -> TestClient:
    settings = Settings(database_url="sqlite+aiosqlite://", create_all=True)
    return TestClient(create_app(settings, parents=_parents(handler)))


def test_brand_claim_manages_its_branch_but_not_a_strangers():
    def handler(request: httpx.Request) -> httpx.Response:
        rid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": rid, "brand_id": "brd_1"})

    with _app_with(handler) as client:
        own = client.put(
            "/v1/inventory/restaurants/rst_dt/stock/itm_a",
            json={"available": 5},
            headers=_admin("brd_1"),  # the claim is the BRAND
        )
        assert own.status_code == 200
        foreign = client.get("/v1/inventory/restaurants/rst_dt/stock", headers=_admin("brd_OTHER"))
        assert foreign.status_code == 404  # someone else's brand — the one 404


def test_catalog_down_is_a_truthful_503_not_a_lying_404():
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("catalog is down")

    with _app_with(down) as client:
        r = client.get("/v1/inventory/restaurants/rst_dt/stock", headers=_admin("brd_1"))
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "1"
        assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
