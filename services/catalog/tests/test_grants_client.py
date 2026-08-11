"""Every branch of the HTTP grants adapter: success, transient retries
(network + 5xx), permanent 4xx, and both exhaustion paths."""

import json

import httpx
import pytest
from catalog.adapters.identity_grants import IdentityGrantsClient
from catalog.domain.ports import GrantRejected, GrantUnavailable


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
        return httpx.Response(step, json={"status": "granted"})

    client = IdentityGrantsClient(
        "http://identity.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_delay=0.0,
    )
    return client, calls


async def test_success_stamps_system_identity():
    client, calls = make([200])
    await client.grant_restaurant_admin(user_id="usr_1", restaurant_id="rst_1")
    request = calls["requests"][0]
    assert request.url.path == "/v1/internal/grants"
    assert request.headers["x-auth-sub"] == "svc:catalog"
    assert request.headers["x-auth-role"] == "system"
    assert request.headers["x-internal-caller"] == "catalog"
    assert json.loads(request.content) == {
        "user_id": "usr_1",
        "role": "restaurant_admin",
        "restaurant_id": "rst_1",
    }


async def test_traceparent_forwarded_only_inside_request_context():
    from smartfood_otel.propagation import set_current_traceparent

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    set_current_traceparent(tp)
    client, calls = make([200])
    await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    # identity's middleware adopts this id → the grant hop logs under the
    # SAME trace_id as the onboarding request that caused it.
    assert calls["requests"][0].headers["traceparent"] == tp


async def test_no_traceparent_header_outside_requests():
    client, calls = make([200])  # module default: no request context set
    await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    assert "traceparent" not in calls["requests"][0].headers


async def test_5xx_retries_then_succeeds():
    client, calls = make([500, 200])
    await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    assert calls["n"] == 2


async def test_network_error_retries_then_succeeds():
    client, calls = make(["boom", 200])
    await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    assert calls["n"] == 2


async def test_4xx_is_permanent_no_retry():
    client, calls = make([409])
    with pytest.raises(GrantRejected):
        await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    assert calls["n"] == 1  # permanent — retrying a refusal is pointless


async def test_network_exhaustion_is_unavailable():
    client, calls = make(["boom"])
    with pytest.raises(GrantUnavailable):
        await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    assert calls["n"] == 3  # all attempts consumed


async def test_5xx_exhaustion_is_unavailable():
    client, calls = make([500])
    with pytest.raises(GrantUnavailable):
        await client.grant_restaurant_admin(user_id="u", restaurant_id="r")
    assert calls["n"] == 3
