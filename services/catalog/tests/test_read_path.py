"""Slice 5 branches: blob/pointer caching, write ordering, invalidation,
version addressing, singleflight, browse filters + page cache, cache-down."""

from catalog.domain.service import _blob_key, _lock_key, _ptr_key
from smartfood_auth import AuthContext, headers_for


def onboard(client, sub="usr_owner", name="Biryani House", cuisines=("pakistani",)):
    customer = headers_for(AuthContext(sub=sub, role="customer"))
    rid = client.post(
        "/v1/restaurants",
        json={"name": name, "city": "springfield", "cuisines": list(cuisines)},
        headers=customer,
    ).json()["id"]
    return rid, headers_for(AuthContext(sub=sub, role="restaurant_admin", restaurant_id=rid))


def seed_menu(client, sub="usr_owner", name="Biryani House", cuisines=("pakistani",),
              tags=("halal",)):
    rid, admin = onboard(client, sub=sub, name=name, cuisines=cuisines)
    cat = client.post(
        f"/v1/restaurants/{rid}/categories", json={"name": "Mains"}, headers=admin
    ).json()
    item = client.post(
        f"/v1/restaurants/{rid}/items",
        json={"category_id": cat["id"], "name": "Biryani", "price_cents": 1200,
              "tags": list(tags)},
        headers=admin,
    ).json()
    return rid, admin, item["id"]


# ── menu blob/pointer ──────────────────────────────────────────────


def test_menu_renders_once_then_serves_from_cache(client, cache):
    rid, _, _ = seed_menu(client)
    first = client.get(f"/v1/menus/{rid}")
    assert first.json()["version"] == 3  # onboard + category + item
    sets = [op for op in cache.ops if op[0] == "set"]
    # Blob-then-pointer: a crash between the two leaves an unused blob,
    # never a pointer at nothing.
    assert sets == [("set", _blob_key(rid, 3)), ("set", _ptr_key(rid))]

    cache.ops.clear()
    second = client.get(f"/v1/menus/{rid}")
    assert second.json() == first.json()
    assert cache.ops == [("get", _ptr_key(rid)), ("get", _blob_key(rid, 3))]  # no render


def test_mutation_deletes_pointer_next_read_rerenders(client, cache):
    rid, admin, item_id = seed_menu(client)
    client.get(f"/v1/menus/{rid}")
    assert _ptr_key(rid) in cache.data

    client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"price_cents": 1500}, headers=admin
    )
    assert _ptr_key(rid) not in cache.data  # invalidated on commit

    fresh = client.get(f"/v1/menus/{rid}").json()
    assert fresh["version"] == 4
    assert fresh["categories"][0]["items"][0]["price_cents"] == 1500
    assert _blob_key(rid, 4) in cache.data


def test_versioned_blob_is_immutable(client, cache):
    rid, admin, item_id = seed_menu(client)
    client.get(f"/v1/menus/{rid}")  # caches v3
    client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"price_cents": 1500}, headers=admin
    )
    old = client.get(f"/v1/menus/{rid}", params={"v": 3})
    assert old.status_code == 200
    assert old.json()["categories"][0]["items"][0]["price_cents"] == 1200  # frozen
    assert old.headers["Cache-Control"] == "public, max-age=604800, immutable"

    current = client.get(f"/v1/menus/{rid}")
    assert current.json()["version"] == 4
    assert current.headers["Cache-Control"] == "public, max-age=5"


def test_versioned_request_for_evicted_old_version_is_404(client, cache):
    rid, admin, item_id = seed_menu(client)
    client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"price_cents": 1500}, headers=admin
    )  # now v4
    cache.data.clear()  # old blobs evicted (TTL/restart)
    r = client.get(f"/v1/menus/{rid}", params={"v": 3})
    assert r.status_code == 404  # only the CURRENT version can be rebuilt
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_versioned_request_current_version_rebuilds(client, cache):
    rid, _, _ = seed_menu(client)
    r = client.get(f"/v1/menus/{rid}", params={"v": 3})  # nothing cached yet
    assert r.status_code == 200
    assert r.headers["Cache-Control"].endswith("immutable")


def test_versioned_unknown_restaurant_is_404(client):
    assert client.get("/v1/menus/rst_ghost", params={"v": 1}).status_code == 404


def test_singleflight_loser_never_releases_foreign_lock(client, cache):
    rid, _, _ = seed_menu(client)
    cache.locks.add(_lock_key(rid))  # someone else is rendering
    r = client.get(f"/v1/menus/{rid}")
    assert r.status_code == 200  # waited, then rendered anyway — never blocked
    assert _lock_key(rid) in cache.locks  # the winner's lock was NOT released


# ── browse ─────────────────────────────────────────────────────────


def test_browse_filters(client, cache):
    seed_menu(client, sub="usr_a", name="Biryani House", cuisines=("pakistani", "bbq"),
              tags=("halal",))
    seed_menu(client, sub="usr_b", name="Burger Barn", cuisines=("burgers",), tags=())

    both = client.get("/v1/restaurants", params={"city": "Springfield"})
    assert [r["name"] for r in both.json()["restaurants"]] == [
        "Biryani House", "Burger Barn",
    ]
    assert both.headers["Cache-Control"] == "public, max-age=30"

    only_pk = client.get(
        "/v1/restaurants", params={"city": "springfield", "cuisine": "pakistani"}
    ).json()
    assert [r["name"] for r in only_pk["restaurants"]] == ["Biryani House"]

    tagged = client.get(
        "/v1/restaurants", params={"city": "springfield", "tag": "halal"}
    ).json()
    assert [r["name"] for r in tagged["restaurants"]] == ["Biryani House"]

    empty_page = client.get(
        "/v1/restaurants", params={"city": "springfield", "page": 5}
    ).json()
    assert empty_page["restaurants"] == []
    assert empty_page["has_more"] is False


def test_browse_has_more_across_pages(client, cache):
    for n in range(21):  # one more than the page size
        onboard(client, sub=f"usr_{n:02}", name=f"Diner {n:02}")
    first = client.get("/v1/restaurants", params={"city": "springfield"}).json()
    assert len(first["restaurants"]) == 20
    assert first["has_more"] is True
    second = client.get("/v1/restaurants", params={"city": "springfield", "page": 1}).json()
    assert len(second["restaurants"]) == 1
    assert second["has_more"] is False


def test_browse_tag_ignores_86d_items(client, cache):
    rid, admin, item_id = seed_menu(client)  # the only halal item
    client.patch(
        f"/v1/restaurants/{rid}/items/{item_id}", json={"available": False}, headers=admin
    )
    cache.data.clear()  # bypass the 60s page cache (staleness is by design)
    r = client.get("/v1/restaurants", params={"city": "springfield", "tag": "halal"})
    assert r.json()["restaurants"] == []  # 86'd items don't advertise


def test_browse_page_is_cached(client, cache):
    seed_menu(client)
    client.get("/v1/restaurants", params={"city": "springfield"})
    sets_before = len([op for op in cache.ops if op[0] == "set"])
    client.get("/v1/restaurants", params={"city": "springfield"})
    sets_after = len([op for op in cache.ops if op[0] == "set"])
    assert sets_after == sets_before  # second page came from cache


def test_browse_validation_branches(client):
    assert client.get("/v1/restaurants").status_code == 422  # city required
    bad = client.get("/v1/restaurants", params={"city": "springfield", "cuisine": "x/y"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_FAILED"
    assert (
        client.get("/v1/restaurants", params={"city": "s", "page": -1}).status_code == 422
    )


# ── cache down: everything still works, nothing 5xxes ──────────────


def test_reads_survive_cache_down(down_client):
    rid, admin, _ = seed_menu(down_client)
    assert down_client.get(f"/v1/menus/{rid}").status_code == 200
    assert down_client.get(f"/v1/menus/{rid}").status_code == 200  # renders again — fine
    assert (
        down_client.get("/v1/restaurants", params={"city": "springfield"}).status_code
        == 200
    )
    # mutations (whose invalidation delete is a no-op) still work too:
    assert (
        down_client.post(
            f"/v1/restaurants/{rid}/categories", json={"name": "Drinks"}, headers=admin
        ).status_code
        == 201
    )
