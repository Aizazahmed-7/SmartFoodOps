import json

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from smartfood_otel import (
    RequestContextMiddleware,
    bind_order,
    extract_traceparent,
    get_logger,
    make_traceparent,
    setup_logging,
    trace_id_of,
)

# ── propagation ────────────────────────────────────────────────────


def test_make_traceparent_is_valid():
    tp = make_traceparent()
    assert extract_traceparent({"traceparent": tp}) == tp
    tid = trace_id_of(tp)
    assert tid is not None and len(tid) == 32


def test_extract_rejects_garbage():
    assert extract_traceparent({}) is None
    assert extract_traceparent({"traceparent": "banana"}) is None
    assert extract_traceparent({"traceparent": "00-zz-11-01"}) is None


# ── logging ────────────────────────────────────────────────────────


def test_log_lines_are_json_with_service_and_context(capsys):
    setup_logging("test-svc")
    structlog.contextvars.clear_contextvars()
    bind_order("ord_123")
    get_logger().info("something happened", extra_field=7)

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["service"] == "test-svc"
    assert line["event"] == "something happened"
    assert line["order_id"] == "ord_123"
    assert line["extra_field"] == 7
    assert line["level"] == "info"
    assert "timestamp" in line


# ── middleware ─────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(RequestContextMiddleware)


@app.get("/ctx")
async def ctx() -> dict:
    return dict(structlog.contextvars.get_contextvars())


client = TestClient(app)


def test_request_id_minted_and_echoed():
    r = client.get("/ctx")
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    assert r.json()["request_id"] == rid
    assert len(r.json()["trace_id"]) == 32


def test_client_supplied_ids_are_kept():
    tp = make_traceparent()
    r = client.get("/ctx", headers={"X-Request-ID": "req-abc", "traceparent": tp})
    assert r.headers["x-request-id"] == "req-abc"
    assert r.json()["request_id"] == "req-abc"
    assert r.json()["trace_id"] == trace_id_of(tp)


def test_malformed_traceparent_replaced_not_trusted():
    r = client.get("/ctx", headers={"traceparent": "not-a-traceparent"})
    assert len(r.json()["trace_id"]) == 32


def test_current_traceparent_outside_requests_is_none():
    from smartfood_otel import current_traceparent

    assert current_traceparent() is None  # pollers/consumers/startup


def test_middleware_sets_current_traceparent():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from smartfood_otel import RequestContextMiddleware, current_traceparent

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/probe")
    async def probe() -> dict:
        return {"traceparent": current_traceparent()}

    inbound = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    body = TestClient(app).get("/probe", headers={"traceparent": inbound}).json()
    assert body["traceparent"] == inbound  # request-scoped and propagated
