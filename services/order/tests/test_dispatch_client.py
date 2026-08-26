"""The worker's dispatch adapter over MockTransport: the pickup-pin cache,
the coordless fallback, and the outcome/unavailability contract."""

import httpx
import pytest
from order.adapters.dispatch_client import FALLBACK_PICKUP, DispatchClient
from order.domain.ports import DispatchUnavailable

PIN = {"id": "rst_1", "name": "Biryani House", "lat": 39.7912, "lon": -89.6644}


def _client(handler) -> DispatchClient:
    return DispatchClient(
        "http://dispatch.test",
        "http://catalog.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _offer(client, order_id="ord_1", attempt=1, exclude=None):
    return await client.find_and_offer(
        order_id,
        user_id="usr_1",
        restaurant_id="rst_1",
        restaurant_name="Biryani House",
        dropoff=(39.8025, -89.6478),
        attempt=attempt,
        exclude=exclude or [],
    )


async def test_offer_fetches_the_pin_once_and_posts_the_cascade_step():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "catalog.test":
            return httpx.Response(200, json=PIN)
        return httpx.Response(
            200, json={"outcome": "offered", "offer_id": "o1", "rider_id": "r1", "timeout_s": 15}
        )

    client = _client(handler)
    first = await _offer(client)
    second = await _offer(client, attempt=2, exclude=["r9"])
    assert first["outcome"] == second["outcome"] == "offered"
    catalog_calls = [r for r in seen if r.url.host == "catalog.test"]
    assert len(catalog_calls) == 1  # the pin is cached per restaurant
    offer_calls = [r for r in seen if r.url.path == "/v1/internal/dispatch/offers"]
    assert len(offer_calls) == 2
    assert offer_calls[0].headers["x-auth-role"] == "system"
    import json

    body = json.loads(offer_calls[1].content)
    assert body["pickup"] == {"lat": 39.7912, "lon": -89.6644}
    assert body["exclude"] == ["r9"] and body["attempt"] == 2


async def test_pinless_restaurant_falls_back_to_city_center():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "catalog.test":
            return httpx.Response(200, json={**PIN, "lat": None, "lon": None})
        import json

        body = json.loads(request.content)
        assert (body["pickup"]["lat"], body["pickup"]["lon"]) == FALLBACK_PICKUP
        return httpx.Response(200, json={"outcome": "no_candidates"})

    assert (await _offer(_client(handler)))["outcome"] == "no_candidates"


async def test_catalog_trouble_is_dispatch_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "catalog.test":
            return httpx.Response(503)
        raise AssertionError("must not reach dispatch")  # pragma: no cover

    with pytest.raises(DispatchUnavailable):
        await _offer(_client(handler))


async def test_the_thin_calls_hit_their_paths():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"outcome": "revoked"})

    client = _client(handler)
    await client.expire_offer("ord_1", offer_id="o1", rider_id="r1")
    await client.unassign_stalled("ord_1", rider_id="r1")
    await client.cancel("ord_1")
    assert paths == [
        "/v1/internal/dispatch/offers/expire",
        "/v1/internal/dispatch/orders/ord_1/unassign",
        "/v1/internal/dispatch/orders/ord_1/cancel",
    ]


async def test_dispatch_errors_and_network_trouble_raise():
    with pytest.raises(DispatchUnavailable):
        await _client(lambda _: httpx.Response(500)).cancel("ord_1")
    with pytest.raises(DispatchUnavailable):
        await _client(lambda _: httpx.Response(422)).cancel("ord_1")

    def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(DispatchUnavailable):
        await _client(refused).cancel("ord_1")
