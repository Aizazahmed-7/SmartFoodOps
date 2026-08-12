"""The system-only HTTP surface: role gates, bounds, replay headers, and
the full lifecycle through routes."""

from payment.domain.ports import GatewayResult, PspUnavailable
from smartfood_auth import AuthContext, headers_for

SYSTEM = headers_for(AuthContext(sub="svc:order-worker", role="system"))
CUSTOMER = headers_for(AuthContext(sub="usr_1", role="customer"))


def auth_body(amount=3446):
    return {"amount_cents": amount, "currency": "USD", "card_token": "tok_ok"}


def test_authorize_then_capture_then_refund_roundtrip(client):
    r = client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    assert r.status_code == 200
    assert r.json()["status"] == "AUTHORIZED"

    captured = client.post("/v1/internal/payments/ord_1/capture", headers=SYSTEM)
    assert captured.json()["status"] == "CAPTURED"

    refunded = client.post("/v1/internal/payments/ord_1/refund", headers=SYSTEM)
    assert refunded.json()["status"] == "REFUNDED"


def test_replay_carries_the_header(client):
    client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    replay = client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    assert replay.status_code == 200
    assert replay.headers["idempotent-replay"] == "true"


def test_decline_replays_identically(client, gateway):
    gateway.script = [GatewayResult(False, "psp_x")]
    first = client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    replay = client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    assert first.status_code == replay.status_code == 402
    assert first.json() == replay.json()
    assert replay.json()["error"]["code"] == "PAYMENT_DECLINED"


def test_same_order_different_amount_is_key_reuse(client):
    client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(3446), headers=SYSTEM)
    r = client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(9999), headers=SYSTEM)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"


def test_capture_without_auth_is_state_conflict(client):
    r = client.post("/v1/internal/payments/ord_ghost/capture", headers=SYSTEM)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_STATE_CONFLICT"


def test_psp_down_is_503_with_retry_after(client, gateway):
    gateway.script = [PspUnavailable("down")]
    r = client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["retry-after"] == "1"


def test_void_roundtrip(client):
    client.post("/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=SYSTEM)
    assert (
        client.post("/v1/internal/payments/ord_1/void", headers=SYSTEM).json()["status"] == "VOIDED"
    )


def test_void_and_refund_map_state_conflicts(client):
    # refund without any capture; void without any auth — both 409 through
    # their own routes' error mapping.
    r = client.post("/v1/internal/payments/ord_ghost/refund", headers=SYSTEM)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ORDER_STATE_CONFLICT"
    r = client.post("/v1/internal/payments/ord_ghost2/void", headers=SYSTEM)
    assert r.status_code == 409


def test_in_progress_money_op_maps_to_409(client, monkeypatch):
    from payment.domain.service import MoneyOpInProgress, PaymentService

    async def busy(self, order_id):
        raise MoneyOpInProgress(order_id)

    monkeypatch.setattr(PaymentService, "void", busy)
    r = client.post("/v1/internal/payments/ord_1/void", headers=SYSTEM)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert r.headers["retry-after"] == "1"


def test_only_system_may_touch_money(client):
    assert (
        client.post(
            "/v1/internal/payments/ord_1/authorize", json=auth_body(), headers=CUSTOMER
        ).status_code
        == 403
    )
    assert client.post("/v1/internal/payments/ord_1/capture", headers=CUSTOMER).status_code == 403


def test_dto_bounds(client):
    bad = auth_body()
    bad["amount_cents"] = 0
    assert (
        client.post("/v1/internal/payments/o/authorize", json=bad, headers=SYSTEM).status_code
        == 422
    )
    bad = auth_body()
    bad["card_token"] = "4111111111111111"  # raw PANs unrepresentable
    assert (
        client.post("/v1/internal/payments/o/authorize", json=bad, headers=SYSTEM).status_code
        == 422
    )
