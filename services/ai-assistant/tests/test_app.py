"""Lifespan and the ops surface."""

from ai_assistant.main import create_app
from fastapi.testclient import TestClient

from .conftest import FakeLlm, settings


def test_healthz_names_the_service(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok", "service": "ai-assistant"}


def test_readyz_checks_the_database(client: TestClient):
    assert client.get("/readyz").status_code == 200


def test_metrics_exposes_the_ai_instruments(client: TestClient):
    client.post(
        "/v1/internal/assistant/echo",
        json={"prompt": "x"},
        headers={"X-Auth-Sub": "svc:test", "X-Auth-Role": "system"},
    )
    body = client.get("/metrics").text
    assert "assistant_response_seconds" in body
    assert 'assistant_tokens_total{direction="prompt"' in body


def test_lifespan_creates_the_schema_and_disposes_the_engine():
    """`create_all` is the test path; containers run Alembic instead."""
    app = create_app(settings(), providers={"anthropic": FakeLlm()})
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
