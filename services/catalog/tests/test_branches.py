"""Brands slice 6 (ADR-0028): the cutover surface — onboarding mints
brand + first branch, branch CRUD, the parent-aware _own, brand-owned
field policing, base-item 86 over the API, fan-out atomicity, and the
boot-time cutover storm."""

import sqlalchemy as sa
from catalog.adapters.repo import CatalogRepo
from catalog.db import outbox, restaurants
from catalog.domain import service as service_module
from smartfood_auth import AuthContext, headers_for

from .test_domain import _create, _service
from .test_effective_menu import _insert_row

BODY = {
    "name": "Biryani House",
    "city": "springfield",
    "cuisines": ["pakistani"],
    "lat": 39.79,
    "lon": -89.65,
}


def _onboard(client, sub="usr_owner"):
    """Mint brand + first branch; return (brand_id, branch_id, owner headers)."""
    customer = headers_for(AuthContext(sub=sub, role="customer"))
    body = client.post("/v1/restaurants", json=BODY, headers=customer).json()
    owner = headers_for(AuthContext(sub=sub, role="restaurant_admin", restaurant_id=body["id"]))
    return body["id"], body["branches"][0]["id"], owner


def _seed_base_item(client, brand_id, owner):
    cat = client.post(
        f"/v1/restaurants/{brand_id}/categories", json={"name": "Mains"}, headers=owner
    ).json()
    item = client.post(
        f"/v1/restaurants/{brand_id}/items",
        json={"category_id": cat["id"], "name": "Biryani", "price_cents": 1200},
        headers=owner,
    ).json()
    return cat["id"], item["id"]


# ── onboarding mints, branch create/list/replay/cap ────────────────


def test_onboarding_replay_returns_the_brand_with_its_branches(client):
    brand_id, branch_id, _ = _onboard(client)
    customer = headers_for(AuthContext(sub="usr_owner", role="customer"))
    replay = client.post("/v1/restaurants", json=BODY, headers=customer)
    assert replay.status_code == 200
    assert replay.json()["id"] == brand_id
    assert [b["id"] for b in replay.json()["branches"]] == [branch_id]


def test_branch_create_list_and_replay_by_label(client):
    brand_id, main_branch, owner = _onboard(client)
    created = client.post(
        f"/v1/restaurants/{brand_id}/branches",
        json={"branch_label": "Airport", "city": "shelbyville", "lat": 39.87, "lon": -89.66},
        headers=owner,
    )
    assert created.status_code == 201
    airport = created.json()
    assert airport["display_name"] == "Biryani House — Airport"
    assert airport["brand_id"] == brand_id
    assert airport["city"] == "shelbyville"

    replay = client.post(
        f"/v1/restaurants/{brand_id}/branches",
        json={"branch_label": "Airport", "city": "shelbyville"},
        headers=owner,
    )
    assert replay.status_code == 200  # idempotent by (brand, label)
    assert replay.json()["id"] == airport["id"]

    public = client.get(f"/v1/restaurants/{brand_id}/branches")
    assert [b["id"] for b in public.json()["branches"]] == sorted(
        [airport["id"], main_branch],
        key=lambda i: ("Airport" if i == airport["id"] else "Main", i),
    )
    assert client.get("/v1/restaurants/rst_ghost/branches").status_code == 404
    # a BRANCH id is not a brand — same 404, no shape leak
    assert client.get(f"/v1/restaurants/{main_branch}/branches").status_code == 404


def test_branch_cap_is_a_409(client, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_BRANCHES", 1)  # Main already exists
    brand_id, _, owner = _onboard(client)
    r = client.post(
        f"/v1/restaurants/{brand_id}/branches",
        json={"branch_label": "One Too Many", "city": "springfield"},
        headers=owner,
    )
    assert r.status_code == 409


# ── the parent-aware _own ──────────────────────────────────────────


def test_own_matrix_brand_branch_stranger_unknown(client):
    brand_id, branch_id, owner = _onboard(client)
    other_brand, other_branch, _ = _onboard(client, sub="usr_other")

    # brand claim edits its branch (parentage arm)
    ok = client.patch(f"/v1/restaurants/{branch_id}", json={"lat": 39.8}, headers=owner)
    assert ok.status_code == 200
    # old-token equality arm: a branch-scoped claim still runs its own shop
    branch_token = headers_for(
        AuthContext(sub="usr_owner", role="restaurant_admin", restaurant_id=branch_id)
    )
    assert (
        client.patch(
            f"/v1/restaurants/{branch_id}", json={"lat": 39.81}, headers=branch_token
        ).status_code
        == 200
    )
    # a stranger's branch and an unknown id share the one 404
    assert (
        client.patch(
            f"/v1/restaurants/{other_branch}", json={"lat": 1.0}, headers=owner
        ).status_code
        == 404
    )
    assert (
        client.patch("/v1/restaurants/rst_ghost", json={"lat": 1.0}, headers=owner).status_code
        == 404
    )


# ── brand-owned fields ─────────────────────────────────────────────


def test_branch_patch_rejects_brand_owned_fields_and_rename_propagates(client, cache):
    brand_id, branch_id, owner = _onboard(client)
    for payload in ({"name": "Rogue Rename"}, {"cuisines": ["thai"]}):
        r = client.patch(f"/v1/restaurants/{branch_id}", json=payload, headers=owner)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_FAILED"

    renamed = client.patch(
        f"/v1/restaurants/{brand_id}", json={"name": "Biryani Palace"}, headers=owner
    )
    assert renamed.status_code == 200
    cache.data.clear()  # bypass the browse page cache
    cards = client.get("/v1/restaurants", params={"city": "springfield"}).json()["restaurants"]
    mine = next(c for c in cards if c["id"] == branch_id)
    assert mine["name"] == "Biryani Palace"  # the copy propagated in-tx
    assert mine["display_name"] == "Biryani Palace — Main"


# ── base-item 86 over the API ──────────────────────────────────────


def test_base_item_86_and_restore_touch_only_this_branch(client, cache):
    brand_id, branch_id, owner = _onboard(client)
    _, item_id = _seed_base_item(client, brand_id, owner)
    sibling = client.post(
        f"/v1/restaurants/{brand_id}/branches",
        json={"branch_label": "Airport", "city": "springfield"},
        headers=owner,
    ).json()

    off = client.put(
        f"/v1/restaurants/{branch_id}/base-items/{item_id}/availability",
        json={"available": False},
        headers=owner,
    )
    assert off.status_code == 200 and off.json()["available"] is False

    here = client.get(f"/v1/menus/{branch_id}").json()
    there = client.get(f"/v1/menus/{sibling['id']}").json()
    assert here["categories"][0]["items"][0]["available"] is False
    assert there["categories"][0]["items"][0]["available"] is True

    on = client.put(
        f"/v1/restaurants/{branch_id}/base-items/{item_id}/availability",
        json={"available": True},
        headers=owner,
    )
    assert on.status_code == 200
    cache.data.clear()
    assert (
        client.get(f"/v1/menus/{branch_id}").json()["categories"][0]["items"][0]["available"]
        is True
    )

    # unknown item and a non-base id share the one 404
    ghost = client.put(
        f"/v1/restaurants/{branch_id}/base-items/itm_ghost/availability",
        json={"available": False},
        headers=owner,
    )
    assert ghost.status_code == 404


def test_branch_files_local_items_into_base_categories(client):
    brand_id, branch_id, owner = _onboard(client)
    base_cat, _ = _seed_base_item(client, brand_id, owner)
    local = client.post(
        f"/v1/restaurants/{branch_id}/items",
        json={"category_id": base_cat, "name": "Truck Chai", "price_cents": 400},
        headers=owner,
    )
    assert local.status_code == 201
    doc = client.get(f"/v1/menus/{branch_id}").json()
    sources = {i["name"]: i["source"] for i in doc["categories"][0]["items"]}
    assert sources == {"Biryani": "base", "Truck Chai": "local"}


# ── fan-out atomicity + the boot storm (domain level) ──────────────


async def test_fanout_failure_rolls_back_every_write(grants, cache, monkeypatch):
    """stage_event dying on the SECOND aggregate must leave no version bump,
    no event, no data write — the fan-out is one transaction or nothing."""
    svc, sessions = await _service(grants, cache)
    brand, _ = await _create(svc)

    real = CatalogRepo.stage_event
    calls = {"n": 0}

    async def failing(self, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # the branch twin of the NEXT mutation
            raise RuntimeError("kaboom mid-fanout")
        return await real(self, **kwargs)

    async with sessions() as s:
        before = (await s.execute(sa.select(sa.func.count()).select_from(outbox))).scalar_one()
    calls["n"] = 0
    monkeypatch.setattr(CatalogRepo, "stage_event", failing)
    try:
        await svc.add_category(brand.id, name="Doomed", rank=0)
        raise AssertionError("must not commit")  # pragma: no cover
    except RuntimeError:
        pass
    monkeypatch.setattr(CatalogRepo, "stage_event", real)
    async with sessions() as s:
        after = (await s.execute(sa.select(sa.func.count()).select_from(outbox))).scalar_one()
        versions = (await s.execute(sa.select(restaurants.c.id, restaurants.c.version))).all()
    assert after == before  # no partial fan-out ever visible
    fresh = await svc.get_menu(brand.id)
    assert fresh["categories"] == []  # the data write rolled back too
    assert all(v == 1 for _, v in versions)  # only the mint's bumps remain


async def test_boot_storm_publishes_each_pending_brand_once(grants, cache):
    """Migration 0007 leaves brands at version 0; the boot converge
    publishes brand + branches exactly once and is a no-op thereafter."""
    svc, sessions = await _service(grants, cache)
    await _insert_row(sessions, rid="brd_m", owner="usr_m", kind="brand")
    await _insert_row(
        sessions, rid="rst_m1", owner="usr_m1", kind="branch", brand_id="brd_m", label="Main"
    )
    await _insert_row(
        sessions, rid="rst_m2", owner="usr_m2", kind="branch", brand_id="brd_m", label="Airport"
    )

    assert await svc.converge_brand_events() == 1
    async with sessions() as s:
        rows = (await s.execute(sa.select(outbox))).all()
    by_aggregate = {e.aggregate_id for e in rows}
    assert by_aggregate == {"brd_m", "rst_m1", "rst_m2"}
    assert all(e.payload["brand_id"] == "brd_m" for e in rows)  # the healing signal

    assert await svc.converge_brand_events() == 0  # version guard: never twice
    async with sessions() as s:
        count = (await s.execute(sa.select(sa.func.count()).select_from(outbox))).scalar_one()
    assert count == 3


def test_branch_create_edge_branches(client, monkeypatch):
    brand_id, main_branch, owner = _onboard(client)
    # a BRANCH is not a brand: creating under it is the same 404
    r = client.post(
        f"/v1/restaurants/{main_branch}/branches",
        json={"branch_label": "Nested", "city": "springfield"},
        headers=owner,
    )
    assert r.status_code == 404
    # an explicit timezone flows through validation
    tz = client.post(
        f"/v1/restaurants/{brand_id}/branches",
        json={"branch_label": "Zoned", "city": "springfield", "timezone": "America/New_York"},
        headers=owner,
    )
    assert tz.status_code == 201

    # losing the (brand, label) insert race adopts the winner (200)
    real = CatalogRepo.get_branch_by_label
    calls = {"n": 0}

    async def racy(self, brand, label):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the race window: pre-check misses the winner
        return await real(self, brand, label)

    monkeypatch.setattr(CatalogRepo, "get_branch_by_label", racy)
    raced = client.post(
        f"/v1/restaurants/{brand_id}/branches",
        json={"branch_label": "Zoned", "city": "springfield"},
        headers=owner,
    )
    assert raced.status_code == 200
    assert raced.json()["id"] == tz.json()["id"]  # adopted, not duplicated


def test_base_availability_on_a_brand_is_the_same_404(client):
    """A brand (no parent) holds no base items to 86 — one 404 shape."""
    brand_id, _, owner = _onboard(client)
    r = client.put(
        f"/v1/restaurants/{brand_id}/base-items/itm_x/availability",
        json={"available": False},
        headers=owner,
    )
    assert r.status_code == 404
