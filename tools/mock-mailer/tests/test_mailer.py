"""mock-mailer behavior: the failure levers ARE the product."""

import random

from fastapi.testclient import TestClient
from mock_mailer.main import MailerSettings, create_app


def _client(settings: MailerSettings | None = None, seed: int = 7) -> TestClient:
    return TestClient(create_app(settings, rng=random.Random(seed)))


def _send(client: TestClient, to: str = "usr_1@customers.smartfood.dev"):
    return client.post(
        "/mailer/send",
        json={
            "to": to,
            "subject": "Your receipt",
            "body": "Paid in full.",
            "attachment_key": "receipts/ord_1.pdf",
        },
    )


def test_healthz():
    assert _client().get("/healthz").json()["service"] == "mock-mailer"


def test_send_lands_in_the_outbox_with_a_message_id():
    client = _client()
    resp = _send(client)
    assert resp.status_code == 202
    message_id = resp.json()["message_id"]
    assert message_id.startswith("msg_")
    (email,) = client.get("/mailer/outbox").json()["emails"]
    assert email["message_id"] == message_id
    assert email["attachment_key"] == "receipts/ord_1.pdf"


def test_attachment_is_optional():
    client = _client()
    resp = client.post("/mailer/send", json={"to": "a@b.dev", "subject": "hi", "body": "text only"})
    assert resp.status_code == 202


def test_bounce_recipient_is_deterministically_rejected():
    client = _client()
    resp = _send(client, to="usr_1@bounce.invalid")
    assert resp.status_code == 400
    assert client.get("/mailer/outbox").json()["emails"] == []


def test_fail_next_burns_down_then_recovers():
    client = _client()
    assert client.post("/admin/fail_next", json={"count": 2}).json() == {"failing_next": 2}
    assert _send(client).status_code == 503
    assert _send(client).status_code == 503
    assert _send(client).status_code == 202  # the counter is spent


def test_fail_rate_rolls_the_dice():
    always = _client(MailerSettings(fail_rate=1.0))
    assert _send(always).status_code == 503
    never = _client(MailerSettings(fail_rate=0.0))
    assert _send(never).status_code == 202


def test_unknown_fields_are_refused():
    resp = _client().post(
        "/mailer/send",
        json={"to": "a@b.dev", "subject": "hi", "body": "x", "bytes": "nope"},
    )
    assert resp.status_code == 422
