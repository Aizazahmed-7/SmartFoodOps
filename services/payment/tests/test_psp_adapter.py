"""The gateway adapter: same-key retries, decline-as-result, error sorting."""

import httpx
import pytest
from payment.adapters.psp import MockPspClient
from payment.domain.ports import PspStateConflict, PspUnavailable


def make(script: list):
    calls = {"n": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        step = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        calls["requests"].append(request)
        if step == "boom":
            raise httpx.ConnectError("boom")
        if isinstance(step, tuple):
            status, body = step
            return httpx.Response(status, json=body)
        return httpx.Response(step, json={"psp_ref": "psp_1", "outcome": "approved"})

    client = MockPspClient(
        "http://psp.test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_delay=0.0,
    )
    return client, calls


async def test_authorize_approved():
    client, calls = make([200])
    result = await client.authorize(
        key="ord_1:auth", amount_cents=3446, currency="USD", card_token="tok_ok"
    )
    assert result.approved and result.psp_ref == "psp_1"
    import json

    sent = json.loads(calls["requests"][0].content)
    assert sent["idempotency_key"] == "ord_1:auth"  # OUR key IS the PSP key


async def test_decline_is_a_result_not_an_exception():
    client, _ = make([(200, {"psp_ref": "psp_1", "outcome": "declined"})])
    result = await client.authorize(
        key="ord_1:auth", amount_cents=100, currency="USD", card_token="tok_decline"
    )
    assert not result.approved


async def test_5xx_and_network_retry_with_the_same_key():
    """FR-22's transport half: every retry carries the identical key."""
    client, calls = make(["boom", 500, 200])
    await client.authorize(key="ord_1:auth", amount_cents=100, currency="USD", card_token="t")
    import json

    keys = {json.loads(r.content)["idempotency_key"] for r in calls["requests"]}
    assert calls["n"] == 3
    assert keys == {"ord_1:auth"}  # never a fresh key mid-operation


async def test_exhaustion_is_unavailable():
    client, calls = make([500])
    with pytest.raises(PspUnavailable):
        await client.authorize(key="k", amount_cents=100, currency="USD", card_token="t")
    assert calls["n"] == 3


async def test_409_is_state_conflict_no_retry():
    client, calls = make([(409, {"error": "cannot capture a voided authorization"})])
    with pytest.raises(PspStateConflict):
        await client.capture(key="ord_1:capture", psp_ref="psp_1")
    assert calls["n"] == 1


async def test_unknown_ref_is_its_own_exception_loud_once():
    """404 = the PSP does not know the ref — a DISTINCT failure from an
    outage, because the domain treats them differently (void converges,
    money ops page). Never retried: the PSP will not learn the ref."""
    from payment.domain.ports import PspUnknownRef

    client, calls = make([(404, {"error": "unknown psp_ref"})])
    with pytest.raises(PspUnknownRef):
        await client.refund(key="k", psp_ref="psp_ghost")
    assert calls["n"] == 1


async def test_other_4xx_is_loud_once():
    client, calls = make([(422, {"error": "bad shape"})])
    with pytest.raises(PspUnavailable):
        await client.refund(key="k", psp_ref="psp_ghost")
    assert calls["n"] == 1


async def test_lifecycle_posts_shape():
    client, calls = make([200])
    await client.void(key="ord_1:void", psp_ref="psp_9")
    assert calls["requests"][0].url.path == "/psp/void"
