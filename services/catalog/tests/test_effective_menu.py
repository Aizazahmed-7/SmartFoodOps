"""Brands slice 1 (ADR-0028): the READ machinery — effective menu union,
source stamping, per-branch 86 overrides, brand-aware browse and pricing.
Rows are hand-inserted (the write surfaces land with the cutover slice)."""

from datetime import UTC, datetime

from catalog.adapters.repo import CatalogRepo
from catalog.db import branch_item_overrides, restaurants
from catalog.domain.service import CatalogService

from .test_domain import _NullSearch, _service

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


async def _insert_row(
    sessions,
    *,
    rid: str,
    owner: str,
    kind: str,
    brand_id: str | None = None,
    label: str | None = None,
    name: str = "Biryani House",
    city: str = "springfield",
):
    async with sessions() as s:
        await s.execute(
            restaurants.insert().values(
                id=rid,
                owner_user_id=owner,
                name=name,
                city=city,
                status="open",
                hours=None,
                timezone="America/Chicago",
                version=0,
                kind=kind,
                brand_id=brand_id,
                branch_label=label,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await s.commit()


async def _add_item(svc: CatalogService, rid: str, category_id: str, name: str, price: int):
    return await svc.add_item(
        rid,
        category_id=category_id,
        fields={
            "name": name,
            "description": None,
            "price_cents": price,
            "currency": "USD",
            "available": True,
            "rank": 0,
        },
        tags=["halal"],
        modifier_groups=[],
    )


async def _brand_world(grants, cache):
    """A brand with a base menu and two branches, one carrying a local menu."""
    svc, sessions = await _service(grants, cache)
    await _insert_row(sessions, rid="brd_1", owner="usr_owner", kind="brand")
    await _insert_row(
        sessions, rid="rst_dt", owner="usr_o2", kind="branch", brand_id="brd_1", label="Downtown"
    )
    await _insert_row(
        sessions, rid="rst_ap", owner="usr_o3", kind="branch", brand_id="brd_1", label="Airport"
    )
    base_cat = await svc.add_category("brd_1", name="Mains", rank=0)
    base_item = await _add_item(svc, "brd_1", base_cat["id"], "Biryani", 1200)
    local_cat = await svc.add_category("rst_dt", name="Downtown Specials", rank=1)
    local_item = await _add_item(svc, "rst_dt", local_cat["id"], "Truck Chai", 400)
    return svc, sessions, base_item["id"], local_item["id"]


async def _override(sessions, branch_id: str, item_id: str):
    async with sessions() as s:
        await s.execute(branch_item_overrides.insert().values(branch_id=branch_id, item_id=item_id))
        await s.commit()


# ── effective render ───────────────────────────────────────────────


async def test_branch_menu_unions_base_and_local_with_sources(grants, cache):
    svc, _, base_item, local_item = await _brand_world(grants, cache)
    doc = await svc.get_menu("rst_dt")
    assert doc["brand_id"] == "brd_1"
    assert doc["display_name"] == "Biryani House — Downtown"
    by_name = {c["name"]: c for c in doc["categories"]}
    assert set(by_name) == {"Mains", "Downtown Specials"}
    biryani = by_name["Mains"]["items"][0]
    chai = by_name["Downtown Specials"]["items"][0]
    assert (biryani["id"], biryani["source"]) == (base_item, "base")
    assert (chai["id"], chai["source"]) == (local_item, "local")

    base_view = await svc.get_menu("brd_1")  # the dashboard's Base tab
    assert base_view["display_name"] == "Biryani House"  # no label — pure name
    assert [c["name"] for c in base_view["categories"]] == ["Mains"]
    assert base_view["categories"][0]["items"][0]["source"] == "local"  # editable here


async def test_branch_86_masks_the_base_item_only_at_that_branch(grants, cache):
    svc, sessions, base_item, _ = await _brand_world(grants, cache)
    await _override(sessions, "rst_dt", base_item)
    cache.data.clear()  # the world changed behind the cache's back

    downtown = await svc.get_menu("rst_dt")
    assert downtown["categories"][0]["items"][0]["available"] is False  # 86'd HERE
    airport = await svc.get_menu("rst_ap")
    assert airport["categories"][0]["items"][0]["available"] is True  # sibling unaffected
    brand = await svc.get_menu("brd_1")
    assert brand["categories"][0]["items"][0]["available"] is True  # base untouched


# ── the money path ─────────────────────────────────────────────────


async def test_pricing_read_scopes_base_items_through_the_branch(grants, cache):
    svc, _, base_item, local_item = await _brand_world(grants, cache)
    body = await svc.pricing_read("rst_dt", [base_item, local_item, "itm_ghost"])
    assert body["restaurant"]["id"] == "rst_dt"
    assert body["restaurant"]["brand_id"] == "brd_1"
    assert body["restaurant"]["display_name"] == "Biryani House — Downtown"
    assert [i["id"] for i in body["items"]] == [base_item, local_item]
    assert body["items"][0]["price_cents"] == 1200  # base price, single source
    assert body["missing_item_ids"] == ["itm_ghost"]


async def test_pricing_read_reports_branch_86_as_unavailable(grants, cache):
    svc, sessions, base_item, _ = await _brand_world(grants, cache)
    await _override(sessions, "rst_dt", base_item)
    here = await svc.pricing_read("rst_dt", [base_item])
    assert here["items"][0]["available"] is False  # pricing refuses the line
    sibling = await svc.pricing_read("rst_ap", [base_item])
    assert sibling["items"][0]["available"] is True


# ── browse ─────────────────────────────────────────────────────────


async def test_browse_hides_brand_rows_and_titles_branches(grants, cache):
    svc, _, _, _ = await _brand_world(grants, cache)
    page = await svc.browse(city="springfield", cuisine=None, tag=None, page=0)
    ids = {r["id"] for r in page["restaurants"]}
    assert ids == {"rst_dt", "rst_ap"}  # the brand row is a template, not a place
    downtown = next(r for r in page["restaurants"] if r["id"] == "rst_dt")
    assert downtown["display_name"] == "Biryani House — Downtown"
    assert downtown["brand_id"] == "brd_1"


async def test_browse_tag_filter_sees_inherited_items_minus_overrides(grants, cache):
    svc, sessions, base_item, _ = await _brand_world(grants, cache)
    tagged = await svc.browse(city="springfield", cuisine=None, tag="halal", page=0)
    assert {r["id"] for r in tagged["restaurants"]} == {"rst_dt", "rst_ap"}  # inherited tag

    await _override(sessions, "rst_ap", base_item)  # Airport 86s its only halal item
    cache.data.clear()  # bypass the 60s page cache
    tagged = await svc.browse(city="springfield", cuisine=None, tag="halal", page=0)
    # Downtown still advertises (its local item is halal-tagged too); Airport
    # inherits nothing sellable under this tag any more.
    assert {r["id"] for r in tagged["restaurants"]} == {"rst_dt"}


# ── torn-read guard through a branch scope ─────────────────────────


async def test_branch_render_retries_when_the_branch_version_moves(grants, cache, monkeypatch):
    """The fan-out (cutover slice) bumps a BRANCH version when its base
    changes; the renderer's re-check must catch that motion mid-read."""
    svc, sessions, _, _ = await _brand_world(grants, cache)

    real = CatalogRepo.get_menu_rows
    calls = {"n": 0}

    async def tearing(self, scope_ids):
        calls["n"] += 1
        rows = await real(self, scope_ids)
        if calls["n"] == 1:  # a base edit's fan-out lands mid-render
            await self._s.execute(
                restaurants.update()
                .where(restaurants.c.id == "rst_dt")
                .values(version=restaurants.c.version + 1)
            )
        return rows

    monkeypatch.setattr(CatalogRepo, "get_menu_rows", tearing)
    doc = await svc.get_menu("rst_dt")
    assert calls["n"] == 2  # first pass torn → re-rendered
    assert doc["brand_id"] == "brd_1"


async def test_null_search_is_a_search_port(grants, cache):
    assert await _NullSearch().search(query="x") == []
