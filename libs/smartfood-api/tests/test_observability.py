"""mount_observability — the /metrics + /readyz ops surface every service gets."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from smartfood_api import mount_observability
from sqlalchemy.ext.asyncio import create_async_engine


def test_metrics_endpoint_serves_prometheus_text():
    app = FastAPI()
    mount_observability(app, engine=create_async_engine("sqlite+aiosqlite://"))
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    assert "http_request_duration_seconds" in resp.text
    assert resp.headers["content-type"].startswith("application/openmetrics-text")


def test_readyz_is_200_when_the_database_answers():
    app = FastAPI()
    mount_observability(app, engine=create_async_engine("sqlite+aiosqlite://"))
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readyz_is_503_when_the_database_is_unreachable():
    """A pool that cannot hand out a connection = not ready. The orchestrator
    drains the pod instead of routing traffic into 500s."""

    class _BadEngine:
        def connect(self):
            class _CM:
                async def __aenter__(self):
                    raise RuntimeError("pool exhausted")

                async def __aexit__(self, *a):
                    return False

            return _CM()

    app = FastAPI()
    mount_observability(app, engine=_BadEngine())
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not ready"}


def test_readyz_without_an_engine_is_liveness_only():
    """The edge-bff owns no database — /readyz degrades to 'the process is up'."""
    app = FastAPI()
    mount_observability(app)  # no engine
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_metrics_scrape_samples_the_db_pool():
    """Capacity's USE side for the database: in_use/idle/size gauges,
    refreshed on every scrape from THIS process's engine."""
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    app = FastAPI()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=AsyncAdaptedQueuePool)
    mount_observability(app, engine=engine)
    body = TestClient(app).get("/metrics").text
    assert 'db_pool_connections{state="size"}' in body
    assert 'db_pool_connections{state="in_use"}' in body
