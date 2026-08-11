"""Slice 6 HTTP branches: param validation/normalization, card assembly,
ranking preservation, vanished-restaurant drop, pagination pass-through."""

from smartfood_auth import AuthContext, headers_for


def _seed_restaurant(client, sub="usr_owner", name="Biryani House"):
    customer = headers_for(AuthContext(sub=sub, role="customer"))
    return client.post(
        "/v1/restaurants",
        json={"name": name, "city": "springfield", "cuisines": ["pakistani", "bbq"]},
        headers=customer,
    ).json()["id"]


def test_search_assembles_cards_and_normalizes_params(client, search_port):
    rid = _seed_restaurant(client)
    search_port.hits = [
        {
            "restaurant_id": rid,
            "score": 0.92,
            "matched_items": [
                {"id": "itm_1", "name": "Chicken Biryani", "price_cents": 1200, "score": 0.92}
            ],
        },
        {"restaurant_id": "rst_vanished", "score": 0.5, "matched_items": []},
    ]
    r = client.get(
        "/v1/search",
        params={
            "q": " biriani ",
            "city": "Springfield",
            "cuisine": "Pakistani",
            "tag": "Halal",
            "page": 1,
        },
    )
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"  # deliberately uncached

    # Params reached the port normalized, page → offset, limit+1 for has_more:
    assert search_port.calls == [
        {
            "query": "biriani",
            "city": "springfield",
            "cuisine": "pakistani",
            "tag": "halal",
            "limit": 21,
            "offset": 20,
        }
    ]

    body = r.json()
    assert body["query"] == "biriani"
    assert body["page"] == 1
    # One card: the vanished restaurant was dropped, not 500'd.
    assert len(body["results"]) == 1
    card = body["results"][0]
    assert card["restaurant"]["name"] == "Biryani House"
    assert card["restaurant"]["cuisines"] == ["bbq", "pakistani"]  # read from DB
    assert card["score"] == 0.92
    assert card["matched_items"][0]["name"] == "Chicken Biryani"


def test_search_preserves_hit_order(client, search_port):
    rid_a = _seed_restaurant(client, sub="usr_a", name="Alpha")
    rid_b = _seed_restaurant(client, sub="usr_b", name="Beta")
    # Port ranks Beta first — assembly must not re-sort by anything else.
    search_port.hits = [
        {"restaurant_id": rid_b, "score": 0.9, "matched_items": []},
        {"restaurant_id": rid_a, "score": 0.3, "matched_items": []},
    ]
    names = [
        c["restaurant"]["name"]
        for c in client.get("/v1/search", params={"q": "anything"}).json()["results"]
    ]
    assert names == ["Beta", "Alpha"]


def test_search_empty_results(client, search_port):
    body = client.get("/v1/search", params={"q": "nothing"}).json()
    assert body["results"] == []
    assert body["has_more"] is False


def test_search_has_more_via_limit_plus_one(client, search_port):
    rid = _seed_restaurant(client)
    # Port returns 21 hits for a 20-sized page → has_more, trimmed to 20.
    search_port.hits = [
        {"restaurant_id": rid, "score": 0.9, "matched_items": []} for _ in range(21)
    ]
    body = client.get("/v1/search", params={"q": "biryani"}).json()
    assert body["has_more"] is True
    assert len(body["results"]) == 20


def test_search_validation_branches(client):
    assert client.get("/v1/search").status_code == 422  # q required
    assert client.get("/v1/search", params={"q": "a"}).status_code == 422  # too short
    assert client.get("/v1/search", params={"q": "x" * 81}).status_code == 422
    bad_filter = client.get("/v1/search", params={"q": "biryani", "cuisine": "x/y"})
    assert bad_filter.status_code == 422
    assert bad_filter.json()["error"]["code"] == "VALIDATION_FAILED"
    assert client.get("/v1/search", params={"q": "biryani", "page": -1}).status_code == 422
