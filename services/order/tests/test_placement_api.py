"""POST /v1/orders through HTTP: the idempotency protocol end to end,
error mapping, header requirements, and the saga hand-off."""

import uuid

from order.adapters.repo import OrderRepo
from smartfood_auth import AuthContext, headers_for

CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))


def _headers(key=None):
    return {**CUSTOMER, "Idempotency-Key": key or uuid.uuid4().hex}


def test_placement_happy_path_202(client, catalog, saga, make_snapshot, make_order_body):
    catalog.snapshot = make_snapshot()
    r = client.post("/v1/orders", json=make_order_body(), headers=_headers())
    assert r.status_code == 202
    placed = r.json()
    assert placed["status"] == "PLACED"
    assert placed["order_id"].startswith("ord_")
    assert saga.started == [placed["order_id"]]  # after-commit hand-off


def test_replay_returns_same_order_no_duplicate(
    client, catalog, saga, make_snapshot, make_order_body
):
    catalog.snapshot = make_snapshot()
    key = uuid.uuid4().hex
    first = client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    replay = client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    assert replay.status_code == 202
    assert replay.json() == first.json()  # stored response, verbatim
    assert replay.headers["idempotent-replay"] == "true"
    assert "idempotent-replay" not in first.headers
    assert saga.started == [first.json()["order_id"]]  # exactly one order


def test_same_key_different_body_is_422_reuse(client, catalog, make_snapshot, make_order_body):
    catalog.snapshot = make_snapshot()
    key = uuid.uuid4().hex
    client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    r = client.post(
        "/v1/orders",
        json=make_order_body(lines=[{"item_id": "itm_a", "qty": 3}]),
        headers=_headers(key),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"


def test_crash_mid_execution_leaves_key_in_progress(
    client, catalog, monkeypatch, make_snapshot, make_order_body
):
    """Unexpected failure (not a business refusal) does NOT release the key:
    the immediate retry gets 409 and the takeover TTL is the recovery."""
    catalog.snapshot = make_snapshot()
    key = uuid.uuid4().hex

    async def exploding_insert(self, **kwargs):
        raise RuntimeError("db died mid-placement")

    monkeypatch.setattr(OrderRepo, "insert_order", exploding_insert)
    import pytest

    with pytest.raises(RuntimeError):  # the request dies mid-flight
        client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    monkeypatch.undo()
    retry = client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert retry.headers["retry-after"] == "1"


def test_missing_idempotency_key_is_422(client, catalog, make_snapshot, make_order_body):
    catalog.snapshot = make_snapshot()
    r = client.post("/v1/orders", json=make_order_body(), headers=CUSTOMER)
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [
        {"field": "Idempotency-Key", "issue": "required header"}
    ]


def test_price_changed_releases_key_for_fresh_confirm(
    client, catalog, make_snapshot, make_order_body
):
    """Version drift → 409 with the current version; the SAME key is then
    immediately usable with the corrected body (deterministic refusals
    free the key — no TTL squatting)."""
    catalog.snapshot = make_snapshot(version=4)
    key = uuid.uuid4().hex
    r = client.post("/v1/orders", json=make_order_body(menu_version=3), headers=_headers(key))
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "PRICE_CHANGED"
    assert error["details"] == [{"field": "menu_version", "issue": "menu is now at version 4"}]
    confirm = client.post("/v1/orders", json=make_order_body(menu_version=4), headers=_headers(key))
    assert confirm.status_code == 202


def test_unknown_address_404_with_detail(client, catalog, make_snapshot, make_order_body):
    catalog.snapshot = make_snapshot()
    r = client.post("/v1/orders", json=make_order_body(address_id="adr_ghost"), headers=_headers())
    assert r.status_code == 404
    assert r.json()["error"]["details"] == [
        {"field": "address_id", "issue": "no such saved address"}
    ]


def test_card_token_pattern_enforced(client, catalog, make_snapshot, make_order_body):
    catalog.snapshot = make_snapshot()
    r = client.post(
        "/v1/orders", json=make_order_body(card_token="4111111111111111"), headers=_headers()
    )
    assert r.status_code == 422  # raw PANs are unrepresentable by shape


def test_paused_restaurant_blocks_placement(client, catalog, make_snapshot, make_order_body):
    catalog.snapshot = make_snapshot(status="paused")
    r = client.post("/v1/orders", json=make_order_body(), headers=_headers())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESTAURANT_CLOSED"


def test_owner_can_place_orders_too(client, catalog, make_snapshot, make_order_body):
    """The Purchaser-gate lesson, applied to placement from day one."""
    catalog.snapshot = make_snapshot()
    owner = headers_for(AuthContext(sub="usr_1", role="restaurant_admin", restaurant_id="rst_9"))
    r = client.post(
        "/v1/orders", json=make_order_body(), headers={**owner, "Idempotency-Key": "k-own"}
    )
    assert r.status_code == 202
