"""Every branch of the identity address adapter — same contract shape as
the catalog client."""

import httpx
import pytest
from order.adapters.identity_client import IdentityClient
from order.domain.ports import AddressNotFound, AddressUnavailable


def make(script: list):
    calls = {"n": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        calls["requests"].append(request)
        if step == "boom":
            raise httpx.ConnectError("boom")
        return httpx.Response(step, json={"id": "adr_1", "label": "home"})

    client = IdentityClient(
        "http://identity.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_delay=0.0,
    )
    return client, calls


async def test_success_stamps_system_identity():
    client, calls = make([200])
    address = await client.get_address("usr_1", "adr_1")
    assert address["id"] == "adr_1"
    request = calls["requests"][0]
    assert request.url.path == "/v1/internal/users/usr_1/addresses/adr_1"
    assert request.headers["x-auth-role"] == "system"
    assert request.headers["x-internal-caller"] == "order"


async def test_traceparent_forwarded_when_in_context():
    from smartfood_otel.propagation import set_current_traceparent

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    set_current_traceparent(tp)
    client, calls = make([200])
    await client.get_address("usr_1", "adr_1")
    assert calls["requests"][0].headers["traceparent"] == tp


async def test_404_is_address_not_found_no_retry():
    client, calls = make([404])
    with pytest.raises(AddressNotFound):
        await client.get_address("usr_1", "adr_ghost")
    assert calls["n"] == 1


async def test_other_4xx_is_permanent():
    client, calls = make([403])
    with pytest.raises(AddressUnavailable):
        await client.get_address("usr_1", "adr_1")
    assert calls["n"] == 1


async def test_transient_failures_retry_then_succeed():
    client, calls = make(["boom", 500, 200])
    assert (await client.get_address("usr_1", "adr_1"))["id"] == "adr_1"
    assert calls["n"] == 3


async def test_exhaustion_is_unavailable():
    client, calls = make([500])
    with pytest.raises(AddressUnavailable):
        await client.get_address("usr_1", "adr_1")
    assert calls["n"] == 3
