"""Every branch of the worker's payment adapter: decline-as-value, the
state-conflict convergence rule, transport failures."""

import httpx
import pytest
from order.adapters.payment_client import PaymentClient, PaymentUnavailable


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

    client = PaymentClient(
        "http://payment.test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return client, calls


async def test_authorize_ok_and_declined_are_values():
    client, calls = make([(200, {"status": "AUTHORIZED"})])
    assert (
        await client.authorize("ord_1", amount_cents=3446, currency="USD", card_token="tok_ok")
        == "ok"
    )
    assert calls["requests"][0].url.path == "/v1/internal/payments/ord_1/authorize"
    assert calls["requests"][0].headers["x-auth-role"] == "system"

    client, _ = make([(402, {"error": {"code": "PAYMENT_DECLINED"}})])
    assert (
        await client.authorize("ord_1", amount_cents=1, currency="USD", card_token="tok_decline")
        == "declined"
    )


async def test_authorize_transport_failures_raise():
    for step in ("boom", 503):
        client, _ = make([step])
        with pytest.raises(PaymentUnavailable):
            await client.authorize("o", amount_cents=1, currency="USD", card_token="tok_ok")


async def test_lifecycle_ok_paths():
    for op in ("void", "capture", "refund"):
        client, calls = make([(200, {"status": "X"})])
        await getattr(client, op)("ord_1")
        assert calls["requests"][0].url.path == f"/v1/internal/payments/ord_1/{op}"


async def test_state_conflict_converges_to_success():
    """Voiding when there is no auth (or it was already voided) is a
    compensation SUCCESS — nothing to undo is the desired end state."""
    client, _ = make([(409, {"error": {"code": "ORDER_STATE_CONFLICT"}})])
    await client.void("ord_1")  # no raise


async def test_other_conflicts_and_transport_failures_raise():
    client, _ = make([(409, {"error": {"code": "IDEMPOTENCY_IN_PROGRESS"}})])
    with pytest.raises(PaymentUnavailable):
        await client.void("ord_1")
    for step in ("boom", 500):
        client, _ = make([step])
        with pytest.raises(PaymentUnavailable):
            await client.capture("ord_1")


async def test_traceparent_forwarded_when_in_context():
    from smartfood_otel.propagation import set_current_traceparent

    tp = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    set_current_traceparent(tp)
    client, calls = make([(200, {})])
    await client.refund("ord_1")
    assert calls["requests"][0].headers["traceparent"] == tp
