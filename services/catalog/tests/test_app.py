def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "catalog"}


def test_error_envelope_installed(client):
    body = client.get("/no-such-route").json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body["error"]
