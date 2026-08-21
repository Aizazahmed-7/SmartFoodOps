"""Every branch of the worker's inventory adapter: outcome mapping,
transport failures, the idempotent lifecycle posts."""

import httpx
import pytest
from order.adapters.inventory_client import InventoryClient, InventoryUnavailable
from order.values import LineSpec


def make(script: list):
    calls = {"n": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        calls["requests"].append(request)
        if step == "boom":
            raise httpx.ConnectError("boom")
        status, body = step if isinstance(step, tuple) else (step, {})
        return httpx.Response(status, json=body)

    client = InventoryClient(
        "http://inventory.test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return client, calls


LINES = [LineSpec(item_id="itm_a", qty=2)]


async def test_reserve_created_and_replayed_are_ok():
    for status in (201, 200):
        client, calls = make([(status, {"order_id": "ord_1", "status": "active"})])
        assert await client.reserve(order_id="ord_1", restaurant_id="rst_1", lines=LINES) == "ok"
        import json

        sent = json.loads(calls["requests"][0].content)
        assert sent["lines"] == [{"item_id": "itm_a", "qty": 2}]
        assert calls["requests"][0].headers["x-auth-role"] == "system"


async def test_reserve_business_outcomes_are_values():
    client, _ = make([(409, {"error": {"code": "ITEM_UNAVAILABLE"}})])
    assert await client.reserve(order_id="o", restaurant_id="r", lines=LINES) == "item_unavailable"
    client, _ = make([(409, {"error": {"code": "RESTAURANT_AT_CAPACITY"}})])
    assert await client.reserve(order_id="o", restaurant_id="r", lines=LINES) == "at_capacity"


async def test_reserve_unexpected_conflict_raises():
    client, _ = make([(409, {"error": {"code": "GRANT_CONFLICT"}})])
    with pytest.raises(InventoryUnavailable):
        await client.reserve(order_id="o", restaurant_id="r", lines=LINES)


async def test_reserve_transport_failures_raise_for_temporal_retry():
    for step in ("boom", 500):
        client, _ = make([step])
        with pytest.raises(InventoryUnavailable):
            await client.reserve(order_id="o", restaurant_id="r", lines=LINES)


async def test_release_and_commit_roundtrip():
    client, calls = make([(200, {"released": True}), (200, {"committed": True})])
    await client.release("ord_1")
    await client.commit("ord_1")
    assert calls["requests"][0].url.path == "/v1/internal/reservations/ord_1/release"
    assert calls["requests"][1].url.path == "/v1/internal/reservations/ord_1/commit"


async def test_release_and_commit_failures_raise():
    client, _ = make([500])
    with pytest.raises(InventoryUnavailable):
        await client.release("ord_1")
    client, _ = make(["boom"])
    with pytest.raises(InventoryUnavailable):
        await client.release("ord_1")
    client, _ = make([500])
    with pytest.raises(InventoryUnavailable):
        await client.commit("ord_1")
    client, _ = make(["boom"])
    with pytest.raises(InventoryUnavailable):
        await client.commit("ord_1")


async def test_traceparent_forwarded_when_in_context():
    from smartfood_otel.propagation import use_traceparent

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    with use_traceparent(tp):
        client, calls = make([(200, {})])
        await client.release("ord_1")
        assert calls["requests"][0].headers["traceparent"] == tp
