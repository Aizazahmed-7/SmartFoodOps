"""Every branch of the catalog snapshot adapter: success + headers/params,
404, permanent 4xx, transient retries (network + 5xx), exhaustion,
traceparent forwarding."""

import httpx
import pytest
from order.adapters.catalog_client import CatalogClient
from order.domain.ports import RestaurantNotFound, SnapshotUnavailable


def make(script: list):
    """Client whose transport plays `script` (status int or "boom" = network
    error; last entry repeats). Returns (client, calls)."""
    calls = {"n": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        calls["requests"].append(request)
        if step == "boom":
            raise httpx.ConnectError("boom")
        return httpx.Response(step, json={"restaurant": {"id": "rst_1"}})

    client = CatalogClient(
        "http://catalog.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_delay=0.0,
    )
    return client, calls


async def test_success_stamps_system_identity_and_params():
    client, calls = make([200])
    snapshot = await client.get_snapshot("rst_1", ["itm_a", "itm_b"])
    assert snapshot == {"restaurant": {"id": "rst_1"}}
    request = calls["requests"][0]
    assert request.url.path == "/v1/internal/restaurants/rst_1/snapshot"
    assert request.url.params.get_list("item_ids") == ["itm_a", "itm_b"]
    assert request.headers["x-auth-sub"] == "svc:order"
    assert request.headers["x-auth-role"] == "system"
    assert request.headers["x-internal-caller"] == "order"


async def test_traceparent_forwarded_only_inside_request_context():
    from smartfood_otel.propagation import set_current_traceparent

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    set_current_traceparent(tp)
    client, calls = make([200])
    await client.get_snapshot("rst_1", ["itm_a"])
    assert calls["requests"][0].headers["traceparent"] == tp


async def test_no_traceparent_outside_requests():
    client, calls = make([200])
    await client.get_snapshot("rst_1", ["itm_a"])
    assert "traceparent" not in calls["requests"][0].headers


async def test_404_is_restaurant_not_found_no_retry():
    client, calls = make([404])
    with pytest.raises(RestaurantNotFound):
        await client.get_snapshot("rst_ghost", ["itm_a"])
    assert calls["n"] == 1


async def test_other_4xx_is_permanent_contract_error():
    client, calls = make([422])
    with pytest.raises(SnapshotUnavailable):
        await client.get_snapshot("rst_1", ["itm_a"])
    assert calls["n"] == 1  # refusals are never retried


async def test_5xx_retries_then_succeeds():
    client, calls = make([500, 200])
    await client.get_snapshot("rst_1", ["itm_a"])
    assert calls["n"] == 2


async def test_network_error_retries_then_succeeds():
    client, calls = make(["boom", 200])
    await client.get_snapshot("rst_1", ["itm_a"])
    assert calls["n"] == 2


async def test_exhaustion_is_unavailable():
    client, calls = make([500])
    with pytest.raises(SnapshotUnavailable):
        await client.get_snapshot("rst_1", ["itm_a"])
    assert calls["n"] == 3  # all attempts consumed
