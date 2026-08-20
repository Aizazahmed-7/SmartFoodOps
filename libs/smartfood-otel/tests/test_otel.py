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


# ── metrics ────────────────────────────────────────────────────────

from smartfood_otel import observe_request, render_metrics  # noqa: E402
from smartfood_otel.metrics import REGISTRY  # noqa: E402


def _count(method: str, status: int) -> float:
    return (
        REGISTRY.get_sample_value(
            "http_request_duration_seconds_count", {"method": method, "status": str(status)}
        )
        or 0.0
    )


def test_observe_request_records_into_the_histogram():
    before = _count("GET", 200)
    observe_request("GET", 200, 0.01)
    assert _count("GET", 200) == before + 1


def test_render_metrics_is_prometheus_exposition():
    observe_request("POST", 201, 0.02)
    body, content_type = render_metrics()
    text = body.decode()
    assert "http_request_duration_seconds" in text
    assert 'method="POST"' in text and 'status="201"' in text
    assert content_type.startswith("text/plain")


def test_middleware_instruments_real_requests_but_skips_ops_paths():
    """A handled request lands in the histogram; the ops endpoints
    (/metrics, /healthz, /readyz) are excluded so a scrape never measures
    itself and heartbeats don't drown real traffic."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    client = TestClient(app)
    before = _count("GET", 200)
    client.get("/ping")  # instrumented → +1
    client.get("/readyz")  # skipped → +0
    assert _count("GET", 200) == before + 1


def test_serve_metrics_exposes_the_registry_over_http():
    """The worker path: no FastAPI, just a bare exposition server. Port 0
    lets the OS choose — the return value tells us where it landed."""
    import urllib.request

    from smartfood_otel import serve_metrics

    port = serve_metrics(0)
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    assert "http_request_duration_seconds" in body
