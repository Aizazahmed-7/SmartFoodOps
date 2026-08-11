import json

import pytest
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


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/boom")
async def boom() -> dict:
    raise ValueError("boom")


client = TestClient(app)


def _events(capsys, event: str) -> list[dict]:
    """Parse captured stdout as JSON log lines, keep the named event."""
    rows = []
    for line in capsys.readouterr().out.strip().splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if parsed.get("event") == event:
            rows.append(parsed)
    return rows


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


def test_every_request_emits_a_completed_line(capsys):
    """The guaranteed per-hop grep match for a request_id/trace_id."""
    setup_logging("access-test")
    client.get("/ctx", headers={"X-Request-ID": "req-log-1"})
    line = _events(capsys, "request completed")[-1]
    assert line["method"] == "GET"
    assert line["path"] == "/ctx"
    assert line["status"] == 200
    assert line["request_id"] == "req-log-1"  # correlation ids ride along
    assert isinstance(line["duration_ms"], float)


def test_healthchecks_stay_quiet(capsys):
    setup_logging("access-test")
    assert client.get("/healthz").status_code == 200
    assert _events(capsys, "request completed") == []


def test_crash_logs_500_and_reraises(capsys):
    setup_logging("access-test")
    with pytest.raises(ValueError):
        client.get("/boom")
    line = _events(capsys, "request completed")[-1]
    assert line["path"] == "/boom"
    assert line["status"] == 500


def test_log_level_env_enables_debug(monkeypatch, capsys):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging("lvl-test")
    get_logger().debug("dbg-visible")
    assert any(
        parsed.get("event") == "dbg-visible"
        for line in capsys.readouterr().out.strip().splitlines()
        if (parsed := json.loads(line))
    )


def test_log_level_unknown_name_falls_back_to_info(monkeypatch, capsys):
    monkeypatch.setenv("LOG_LEVEL", "BANANA")
    setup_logging("lvl-test")
    get_logger().debug("dbg-hidden")
    assert "dbg-hidden" not in capsys.readouterr().out


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
