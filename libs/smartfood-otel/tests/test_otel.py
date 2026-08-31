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


def test_stream_prefixes_are_logged_but_never_measured(capsys):
    """A held connection's duration is policy, not latency: stream paths
    keep their completion log line but stay OUT of the histogram (and the
    span) — the fix for notification's p95 pinning at the top bucket
    whenever a 15-minute bell stream closed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as TC

    stream_app = FastAPI()
    stream_app.add_middleware(RequestContextMiddleware, stream_prefixes=("/v1/stream/",))

    @stream_app.get("/v1/stream/{channel}")
    async def stream(channel: str) -> dict:
        return {"held": channel}

    @stream_app.get("/v1/plain")
    async def plain() -> dict:
        return {"ok": True}

    setup_logging("stream-test")
    with TC(stream_app) as c:
        before = _count("GET", 200)
        assert c.get("/v1/stream/ord_42").status_code == 200
        assert _count("GET", 200) == before  # no histogram sample for the stream
        assert c.get("/v1/plain").status_code == 200
        assert _count("GET", 200) == before + 1  # plain paths still measured
    lines = _events(capsys, "request completed")
    assert [entry["path"] for entry in lines] == ["/v1/stream/ord_42", "/v1/plain"]
    # the stream still LOGS its completion — one line per close is the
    # debugging trail; only the measurement is suppressed.


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
    assert content_type.startswith("application/openmetrics-text")


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


# ── tracing: OpenTelemetry SDK, our propagation contract on top ────


@pytest.fixture()
def traced():
    """Armed SDK with an in-memory exporter (synchronous processor —
    spans land deterministically); always disarmed after."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from smartfood_otel import reset_tracing, setup_tracing

    exporter = InMemorySpanExporter()
    setup_tracing("test-svc", "", exporter=exporter)
    yield exporter
    reset_tracing()


def _hex(trace_or_span_id: int, width: int) -> str:
    return format(trace_or_span_id, f"0{width}x")


def test_unconfigured_tracer_is_a_noop():
    from smartfood_otel import get_tracer

    with get_tracer().start_as_current_span("x") as span:
        assert not span.is_recording()  # free, exports nowhere


def test_extract_context_degrades_on_garbage():
    from smartfood_otel import extract_context, get_tracer

    ctx = extract_context({"traceparent": "not-a-traceparent"})
    with get_tracer().start_as_current_span("x", context=ctx) as span:
        assert not span.is_recording()  # a root in a no-op world, never an error


def test_middleware_exports_a_server_span_with_the_callers_parent(traced):
    from smartfood_otel import span_id_of, trace_id_of

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    incoming = make_traceparent()
    TestClient(app).get("/ping", headers={"traceparent": incoming})
    spans = traced.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "GET /ping"
    assert _hex(span.context.trace_id, 32) == trace_id_of(incoming)  # same trace…
    assert span.parent is not None
    assert _hex(span.parent.span_id, 16) == span_id_of(incoming)  # …parented on the caller
    assert _hex(span.context.span_id, 16) != span_id_of(incoming)  # …with its OWN hop id
    assert span.attributes is not None and span.attributes["http.status_code"] == 200


def test_middleware_marks_5xx_spans_as_errors(traced):
    from fastapi.responses import JSONResponse
    from opentelemetry.trace import StatusCode

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    async def boom() -> JSONResponse:
        return JSONResponse({"error": "down"}, status_code=503)

    TestClient(app).get("/boom")
    span = traced.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes is not None and span.attributes["http.status_code"] == 503


def test_middleware_skips_spans_for_ops_paths(traced):
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    TestClient(app).get("/readyz")
    assert traced.get_finished_spans() == ()  # a probe must not trace itself


def test_untraced_middleware_passes_the_callers_header_through():
    """Tracing OFF: no span exists, so the hop forwards the INCOMING
    traceparent untouched — cross-service log continuity, the pre-tracing
    contract."""
    from smartfood_otel import current_traceparent

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/probe")
    async def probe() -> dict:
        return {"traceparent": current_traceparent()}

    inbound = "00-" + "ab" * 16 + "-" + "cd" * 8 + "-01"
    body = TestClient(app).get("/probe", headers={"traceparent": inbound}).json()
    # OTel's no-op tracer still PROPAGATES the remote context (a
    # NonRecordingSpan wrapping the caller's ids) — so an untraced hop
    # forwards the header verbatim. The pre-tracing contract, now
    # provided by the SDK itself.
    assert body["traceparent"] == inbound


def test_setup_with_an_endpoint_arms_the_live_otlp_path():
    """The production branch: an endpoint (no injected exporter) builds the
    OTLP/HTTP exporter + batch processor. Constructing does no network I/O,
    so this is safe offline; reset flushes into the void and shrugs."""
    from smartfood_otel import get_tracer, reset_tracing, setup_tracing

    setup_tracing("test-live", "http://collector-that-does-not-exist:4318")
    try:
        with get_tracer().start_as_current_span("x") as span:
            assert span.is_recording()  # armed, batching for export
    finally:
        reset_tracing()  # shutdown's flush fails to connect and drops — by design


def test_tracing_armed_gauge_reports_the_real_state():
    """Config drift's only witness. The gauge must read 0 for an unarmed
    process (empty endpoint = the silent-untraced case this exists to
    catch), 1 once armed, and 0 again after a reset — anything else and the
    TracingDisarmed alert either misses drift or cries wolf."""
    from smartfood_otel import REGISTRY, reset_tracing, setup_tracing

    def armed():
        return REGISTRY.get_sample_value("tracing_armed")

    reset_tracing()
    setup_tracing("test-off", "")  # no endpoint, no exporter → stays off
    assert armed() == 0
    setup_tracing("test-live", "http://collector-that-does-not-exist:4318")
    try:
        assert armed() == 1
    finally:
        reset_tracing()
    assert armed() == 0


def test_instrumentation_guards_are_idempotent():
    """Double-arm and cold-reset both no-op: the process-global
    monkeypatches must never stack or unwind twice."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from smartfood_otel import reset_tracing, setup_tracing

    reset_tracing()  # never armed → uninstrument early-returns
    setup_tracing("test-a", "", exporter=InMemorySpanExporter())
    try:
        setup_tracing("test-b", "", exporter=InMemorySpanExporter())  # second arm → early-return
    finally:
        reset_tracing()


def test_noise_filter_drops_only_parentless_sqlalchemy_spans():
    """The poller's idle SELECT must not mint one-span traces; a SQL span
    UNDER a request, and any root span of OURS, must pass untouched."""
    from types import SimpleNamespace

    from smartfood_otel.tracing import _DropParentlessInfraSpans

    class Delegate:
        def __init__(self):
            self.ended, self.started, self.shut, self.flushed = [], [], False, False

        def on_start(self, span, parent_context=None):
            self.started.append(span)

        def on_end(self, span):
            self.ended.append(span)

        def shutdown(self):
            self.shut = True

        def force_flush(self, timeout_millis=30000):
            self.flushed = True
            return True

    def fake(parent, scope_name):
        # Duck-typed stand-in — the filter reads only .parent and
        # .instrumentation_scope.name, so a namespace suffices.
        return SimpleNamespace(  # type: ignore[return-value]
            parent=parent, instrumentation_scope=SimpleNamespace(name=scope_name)
        )

    delegate = Delegate()
    f = _DropParentlessInfraSpans(delegate)  # type: ignore[arg-type]
    f.on_start(fake(None, "x"))  # pyright: ignore[reportArgumentType]
    f.on_end(fake(None, "opentelemetry.instrumentation.sqlalchemy"))  # pyright: ignore[reportArgumentType]
    f.on_end(fake("parent", "opentelemetry.instrumentation.sqlalchemy"))  # pyright: ignore[reportArgumentType]
    f.on_end(fake(None, "smartfood-otel"))  # pyright: ignore[reportArgumentType]
    assert len(delegate.ended) == 2
    assert len(delegate.started) == 1
    assert f.force_flush() and delegate.flushed
    f.shutdown()
    assert delegate.shut


def test_traced_requests_pin_an_exemplar_to_the_histogram(traced):
    """The metrics→traces bridge: an observation made inside a recording
    span carries its trace_id as an OpenMetrics exemplar — the dot in
    Grafana that clicks through to Jaeger."""
    from smartfood_otel import render_metrics, trace_id_of

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    incoming = make_traceparent()
    TestClient(app).get("/ping", headers={"traceparent": incoming})
    body = render_metrics()[0].decode()
    assert f'# {{trace_id="{trace_id_of(incoming)}"}}' in body
