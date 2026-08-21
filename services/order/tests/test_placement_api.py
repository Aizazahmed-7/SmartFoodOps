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
    assert saga.placed == [placed["order_id"]]  # after-commit hand-off


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
    assert saga.placed == [first.json()["order_id"]]  # exactly one order


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


def test_crash_mid_execution_leaves_nothing_and_the_retry_succeeds(
    client, catalog, monkeypatch, make_snapshot, make_order_body
):
    """A crash before the order commits leaves NO record and NO lock
    (ADR-0024) — the retry with the same key simply runs placement again
    and lands on the same derived id. No 409, no takeover window."""
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
    assert retry.status_code == 202
    assert retry.json()["status"] == "PLACED"


def test_slow_worker_still_answers_202(client, catalog, saga, make_snapshot, make_order_body):
    """The saga is durable but has not written the row inside the await
    budget. The customer's answer is unchanged — 202 with the order id they
    will be able to poll — because a 5xx here would ask them to re-order
    against a workflow that is very much alive (ADR-0023)."""
    catalog.snapshot = make_snapshot()
    saga.pending = True
    r = client.post("/v1/orders", json=make_order_body(), headers=_headers())
    assert r.status_code == 202
    assert r.json()["status"] == "PLACED"
    # Read-your-writes is briefly suspended: the row is not there yet.
    assert client.get(f"/v1/orders/{r.json()['order_id']}", headers=CUSTOMER).status_code == 404


def test_temporal_outage_is_503_and_the_retry_converges(
    client, catalog, saga, make_snapshot, make_order_body
):
    """ADR-0023's accepted cost, ADR-0024's payoff, in one test: the
    orchestrator's outage is a 503 with nothing written anywhere — so the
    retry with the same key just places normally, onto the same derived
    order id. One order, no waiting room."""
    from order.domain.ports import SagaUnavailable

    catalog.snapshot = make_snapshot()
    key = uuid.uuid4().hex
    saga.fail_place = SagaUnavailable("temporal down")

    r = client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["retry-after"] == "1"

    saga.fail_place = None
    retry = client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    assert retry.status_code == 202
    assert "idempotent-replay" not in retry.headers  # a FIRST answer, not a replay
    replay = client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    assert replay.json() == retry.json()  # …and THIS one is the replay
    assert replay.headers["idempotent-replay"] == "true"


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
    immediately usable with the corrected body — a refusal writes nothing,
    so there is nothing to free (ADR-0024). NOTE: the corrected body is a
    DIFFERENT body under the same key, and that is legal here because no
    order exists yet — the hash guard only protects created orders."""
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


def test_placement_metrics_classify_placed_and_replay(
    client, catalog, saga, make_snapshot, make_order_body
):
    """The SLO histogram is labelled by OUTCOME so replays (microseconds)
    cannot flatter the p95 the fresh path is measured against."""
    from smartfood_otel import REGISTRY

    def count(outcome):
        return (
            REGISTRY.get_sample_value("order_placement_seconds_count", {"outcome": outcome}) or 0.0
        )

    catalog.snapshot = make_snapshot()
    placed_before, replay_before = count("placed"), count("replay")
    key = uuid.uuid4().hex
    client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    client.post("/v1/orders", json=make_order_body(), headers=_headers(key))
    assert count("placed") == placed_before + 1
    assert count("replay") == replay_before + 1
