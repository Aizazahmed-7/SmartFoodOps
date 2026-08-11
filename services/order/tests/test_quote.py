"""POST /v1/quote: happy path, the full error-code mapping, DTO bounds,
role gates, and the id-dedupe contract with catalog."""

from order.domain.ports import RestaurantNotFound, SnapshotUnavailable
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))
RIDER = headers_for(AuthContext(sub="usr_2", role="rider"))


def snapshot(*, status="open", version=3):
    return {
        "restaurant": {
            "id": "rst_1",
            "name": "Biryani House",
            "city": "springfield",
            "status": status,
            "version": version,
        },
        "items": [
            {
                "id": "itm_a",
                "name": "Chicken Biryani",
                "price_cents": 1200,
                "currency": "USD",
                "available": True,
                "modifier_groups": [
                    {
                        "id": "grp_size",
                        "name": "Size",
                        "min_select": 1,
                        "max_select": 1,
                        "options": [
                            {"id": "opt_reg", "name": "Regular", "price_delta_cents": 0},
                            {"id": "opt_lg", "name": "Large", "price_delta_cents": 300},
                        ],
                    }
                ],
            },
            {
                "id": "itm_b",
                "name": "Raita",
                "price_cents": 200,
                "currency": "USD",
                "available": True,
                "modifier_groups": [],
            },
        ],
        "missing_item_ids": [],
    }


def body(lines=None):
    return {
        "restaurant_id": "rst_1",
        "lines": lines
        or [
            {
                "item_id": "itm_a",
                "qty": 2,
                "options": [{"group_id": "grp_size", "option_id": "opt_lg"}],
            },
            {"item_id": "itm_b", "qty": 1},
        ],
    }


def test_quote_happy_path(client, catalog):
    catalog.snapshot = snapshot()
    r = client.post("/v1/quote", json=body(), headers=CUSTOMER)
    assert r.status_code == 200
    priced = r.json()
    # (1200 + 300) * 2 + 200
    assert priced["totals"]["subtotal_cents"] == 3200
    assert priced["totals"]["fee_cents"] == 199
    assert priced["totals"]["tax_cents"] == 3200 * 825 // 10_000
    assert priced["totals"]["total_cents"] == sum(
        (
            priced["totals"]["subtotal_cents"],
            priced["totals"]["fee_cents"],
            priced["totals"]["tax_cents"],
        )
    )
    assert priced["menu_version"] == 3
    assert priced["lines"][0]["options"][0]["name"] == "Large"


def test_quote_dedupes_item_ids_for_catalog(client, catalog):
    catalog.snapshot = snapshot()
    lines = [
        {
            "item_id": "itm_a",
            "qty": 1,
            "options": [{"group_id": "grp_size", "option_id": "opt_reg"}],
        },
        {
            "item_id": "itm_a",
            "qty": 2,
            "options": [{"group_id": "grp_size", "option_id": "opt_lg"}],
        },
        {"item_id": "itm_b", "qty": 1},
    ]
    assert client.post("/v1/quote", json=body(lines), headers=CUSTOMER).status_code == 200
    assert catalog.calls == [("rst_1", ["itm_a", "itm_b"])]  # deduped, order kept


def test_unknown_restaurant_404(client, catalog):
    catalog.fail_with = RestaurantNotFound("rst_1")
    r = client.post("/v1/quote", json=body(), headers=CUSTOMER)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_paused_restaurant_maps_to_restaurant_closed(client, catalog):
    catalog.snapshot = snapshot(status="paused")
    r = client.post("/v1/quote", json=body(), headers=CUSTOMER)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESTAURANT_CLOSED"


def test_unavailable_items_map_with_details(client, catalog):
    snap = snapshot()
    snap["items"][0]["available"] = False
    catalog.snapshot = snap
    r = client.post("/v1/quote", json=body(), headers=CUSTOMER)
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "ITEM_UNAVAILABLE"
    assert error["details"] == [{"item_id": "itm_a", "issue": "unavailable"}]


def test_invalid_selection_maps_to_422(client, catalog):
    catalog.snapshot = snapshot()
    lines = [{"item_id": "itm_a", "qty": 1}]  # required Size unpicked
    r = client.post("/v1/quote", json=body(lines), headers=CUSTOMER)
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "at least 1" in error["details"][0]["issue"]


def test_catalog_down_maps_to_503_with_retry_after(client, catalog):
    catalog.fail_with = SnapshotUnavailable("down")
    r = client.post("/v1/quote", json=body(), headers=CUSTOMER)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["retry-after"] == "1"


def test_role_gates(client, catalog):
    catalog.snapshot = snapshot()
    assert client.post("/v1/quote", json=body(), headers=RIDER).status_code == 403
    system = headers_for(AuthContext(sub="svc:x", role="system"))
    assert client.post("/v1/quote", json=body(), headers=system).status_code == 200
    # A promoted owner still orders dinner (found live: demo owner 403'd).
    owner = headers_for(AuthContext(sub="usr_3", role="restaurant_admin", restaurant_id="rst_9"))
    assert client.post("/v1/quote", json=body(), headers=owner).status_code == 200


def test_dto_bounds(client, catalog):
    catalog.snapshot = snapshot()
    bad = body()
    bad["lines"] = []
    assert client.post("/v1/quote", json=bad, headers=CUSTOMER).status_code == 422
    bad = body([{"item_id": "itm_a", "qty": 0}])
    assert client.post("/v1/quote", json=bad, headers=CUSTOMER).status_code == 422
    bad = body()
    bad["surprise"] = True
    assert client.post("/v1/quote", json=bad, headers=CUSTOMER).status_code == 422
    assert catalog.calls == []  # every rejection happened before any I/O
