"""Every branch of the fake bank: magic tokens, knobs (scripted RNG),
replay-by-key, the authorization lifecycle, bounds."""

import random

from fastapi.testclient import TestClient
from mock_psp.main import PspSettings, create_app


def client(*, rng=None, **settings) -> TestClient:
    return TestClient(create_app(PspSettings(timeout_sleep_seconds=0.01, **settings), rng=rng))


def auth_body(key="k1", token="tok_ok"):
    return {"idempotency_key": key, "amount_cents": 3446, "currency": "USD", "card_token": token}


class ScriptedRandom(random.Random):
    """random() returns scripted values in order (repeats the last)."""

    def __init__(self, *values: float):
        super().__init__()
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]


def test_authorize_approves_and_replays():
    c = client()
    first = c.post("/psp/authorize", json=auth_body()).json()
    assert first["outcome"] == "approved"
    assert first["psp_ref"].startswith("psp_")
    replay = c.post("/psp/authorize", json=auth_body()).json()
    assert replay == first  # same key → identical answer, no second auth


def test_tok_decline_is_deterministic_even_with_zero_knobs():
    c = client()
    r = c.post("/psp/authorize", json=auth_body(token="tok_decline")).json()
    assert r["outcome"] == "declined"


def test_tok_timeout_records_before_hanging():
    """The caller times out; the retry with the SAME key gets the recorded
    approval instantly — N timeouts still yield exactly one authorization."""
    c = client()
    slow = c.post("/psp/authorize", json=auth_body(token="tok_timeout")).json()
    assert slow["outcome"] == "approved"  # (test sleeps only 10ms)
    replay = c.post("/psp/authorize", json=auth_body(token="tok_timeout")).json()
    assert replay == slow


def test_tok_unknown_500_then_same_key_resolves():
    c = client()
    first = c.post("/psp/authorize", json=auth_body(token="tok_unknown"))
    assert first.status_code == 500  # ambiguous outcome...
    resolved = c.post("/psp/authorize", json=auth_body(token="tok_unknown"))
    assert resolved.status_code == 200  # ...resolved by the key replay
    assert resolved.json()["outcome"] == "approved"


def test_knobs_roll_the_dice_in_order():
    # decline < 0.2 | timeout < 0.4 | unknown < 0.6 | else approve
    c = client(
        rng=ScriptedRandom(0.1, 0.3, 0.5, 0.9),
        decline_rate=0.2,
        timeout_rate=0.2,
        unknown_outcome_rate=0.2,
    )
    assert c.post("/psp/authorize", json=auth_body("k1")).json()["outcome"] == "declined"
    # 0.3 lands in the timeout band (records, hangs 10ms, then answers)
    assert c.post("/psp/authorize", json=auth_body("k2")).json()["outcome"] == "approved"
    assert c.post("/psp/authorize", json=auth_body("k3")).status_code == 500  # unknown path
    assert c.post("/psp/authorize", json=auth_body("k4")).json()["outcome"] == "approved"


def test_latency_knob_path_runs():
    c = client(latency_ms_p50=1)
    assert c.post("/psp/authorize", json=auth_body()).status_code == 200


def test_capture_void_refund_lifecycle():
    c = client()
    ref = c.post("/psp/authorize", json=auth_body("ka")).json()["psp_ref"]

    captured = c.post("/psp/capture", json={"idempotency_key": "kc", "psp_ref": ref}).json()
    assert captured["outcome"] == "captured"
    # replay of the capture — not a state error
    again = c.post("/psp/capture", json={"idempotency_key": "kc", "psp_ref": ref}).json()
    assert again == captured
    # a FRESH-key capture of an already-captured ref is a state conflict
    conflict = c.post("/psp/capture", json={"idempotency_key": "kc2", "psp_ref": ref})
    assert conflict.status_code == 409

    refunded = c.post("/psp/refund", json={"idempotency_key": "kr", "psp_ref": ref}).json()
    assert refunded["outcome"] == "refunded"


def test_void_only_from_authorized():
    c = client()
    ref = c.post("/psp/authorize", json=auth_body("ka")).json()["psp_ref"]
    assert (
        c.post("/psp/void", json={"idempotency_key": "kv", "psp_ref": ref}).json()["outcome"]
        == "voided"
    )
    # refunding a voided auth is a conflict; unknown ref is 404
    assert c.post("/psp/refund", json={"idempotency_key": "kr", "psp_ref": ref}).status_code == 409
    assert (
        c.post("/psp/refund", json={"idempotency_key": "kx", "psp_ref": "psp_ghost"}).status_code
        == 404
    )


def test_dto_bounds():
    c = client()
    bad = auth_body()
    bad["amount_cents"] = 0
    assert c.post("/psp/authorize", json=bad).status_code == 422
    bad = auth_body()
    bad["currency"] = "usd"
    assert c.post("/psp/authorize", json=bad).status_code == 422


def test_healthz():
    assert client().get("/healthz").json()["service"] == "mock-psp"
