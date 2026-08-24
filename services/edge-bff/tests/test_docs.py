"""The gateway's merged OpenAPI: allowlist filtering, auth marking, schema
collision handling, caching, and the partial-doc degraded mode."""

import httpx
import pytest
from edge_bff.config import Settings
from edge_bff.main import create_app
from fastapi.testclient import TestClient
from smartfood_auth import JwksVerifier

IDENTITY_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "identity"},
    "paths": {
        "/healthz": {"get": {"summary": "hz"}},  # not routed → filtered
        "/v1/internal/grants": {"post": {"summary": "grant"}},  # internal → filtered
        "/v1/auth/login": {"post": {"summary": "login"}},
        "/v1/auth/me": {
            "get": {
                "summary": "me",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        }
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "ValidationError": {"type": "object", "title": "shared-identical"},
            "Thing": {"type": "object", "properties": {"a": {"type": "string"}}},
        }
    },
}

CATALOG_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "catalog"},
    "paths": {
        "/v1/restaurants": {"get": {"summary": "browse"}, "post": {"summary": "onboard"}},
        "/v1/restaurants/{restaurant_id}/items": {
            "parameters": [{"name": "restaurant_id", "in": "path"}],  # non-method key
            "post": {
                "summary": "add item",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                    }
                },
            },
        },
        "/v1/search": {"get": {"summary": "search"}},
    },
    "components": {
        "schemas": {
            "ValidationError": {"type": "object", "title": "shared-identical"},
            "Thing": {"type": "object", "properties": {"b": {"type": "integer"}}},  # ≠ identity's
        }
    },
}


@pytest.fixture()
def fetches():
    return {"n": 0}


def make_client(settings: Settings, fetches) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            fetches["n"] += 1
            if request.url.host == "identity.svc":
                return httpx.Response(200, json=IDENTITY_SPEC)
            if request.url.host == "catalog.svc":
                return httpx.Response(200, json=CATALOG_SPEC)
            raise httpx.ConnectError("down")
        raise httpx.ConnectError("docs tests never proxy")

    transport = httpx.MockTransport(handler)
    app = create_app(
        settings,
        http=httpx.AsyncClient(transport=transport),
        verifier=JwksVerifier(
            settings.identity_jwks_url,
            issuer=settings.token_issuer,
            audience="sfo-api",
            http=httpx.AsyncClient(transport=transport),
        ),
    )
    return TestClient(app)


@pytest.fixture()
def client(fetches):
    """identity + catalog answer; inventory + order + notification are DOWN."""
    return make_client(
        Settings(
            identity_base_url="http://identity.svc",
            catalog_base_url="http://catalog.svc",
            order_base_url="http://down.test",
            identity_jwks_url="http://identity.svc/.well-known/jwks.json",
            token_issuer="http://identity.svc",
        ),
        fetches,
    )


@pytest.fixture()
def healthy_client(fetches):
    """Every upstream resolves (order/inventory/notification aliased for the test)."""
    return make_client(
        Settings(
            identity_base_url="http://identity.svc",
            catalog_base_url="http://catalog.svc",
            order_base_url="http://identity.svc",
            inventory_base_url="http://catalog.svc",
            notification_base_url="http://identity.svc",
            analytics_base_url="http://identity.svc",
            identity_jwks_url="http://identity.svc/.well-known/jwks.json",
            token_issuer="http://identity.svc",
        ),
        fetches,
    )


def test_merged_doc_is_allowlist_filtered(client):
    doc = client.get("/openapi.json").json()
    assert set(doc["paths"]) == {
        "/v1/auth/login",
        "/v1/auth/me",
        "/v1/restaurants",
        "/v1/restaurants/{restaurant_id}/items",
        "/v1/search",
    }  # /healthz and /v1/internal/* never appear
    assert doc["servers"] == [{"url": "/"}]
    assert doc["info"]["title"] == "SmartFoodOps API"


def test_auth_marking_follows_the_route_table(client):
    doc = client.get("/openapi.json").json()
    assert "security" not in doc["paths"]["/v1/auth/login"]["post"]  # public
    assert doc["paths"]["/v1/auth/me"]["get"]["security"] == [{"bearerAuth": []}]
    assert "security" not in doc["paths"]["/v1/restaurants"]["get"]  # public_read GET
    assert doc["paths"]["/v1/restaurants"]["post"]["security"] == [{"bearerAuth": []}]
    assert doc["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"


def test_schema_collisions_rename_and_rewrite_refs(client):
    doc = client.get("/openapi.json").json()
    schemas = doc["components"]["schemas"]
    assert schemas["ValidationError"]["title"] == "shared-identical"  # deduped, once
    # catalog (first alphabetically) keeps "Thing"; identity's is renamed:
    assert schemas["Thing"]["properties"] == {"b": {"type": "integer"}}
    assert schemas["Identity_Thing"]["properties"] == {"a": {"type": "string"}}
    me_ref = doc["paths"]["/v1/auth/me"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert me_ref == "#/components/schemas/Identity_Thing"  # ref followed the rename
    item_ref = doc["paths"]["/v1/restaurants/{restaurant_id}/items"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    assert item_ref == "#/components/schemas/Thing"  # catalog's untouched


def test_partial_doc_flags_missing_and_is_not_cached(client, fetches):
    first = client.get("/openapi.json").json()
    assert first["x-unavailable-upstreams"] == [
        "analytics_base_url",
        "inventory_base_url",
        "notification_base_url",
        "order_base_url",
    ]
    after_first = fetches["n"]
    client.get("/openapi.json")  # partial → rebuilt (self-heals when they return)
    assert fetches["n"] > after_first


def test_complete_doc_is_cached_until_refresh(healthy_client, fetches):
    doc = healthy_client.get("/openapi.json").json()
    assert "x-unavailable-upstreams" not in doc
    after_first = fetches["n"]
    healthy_client.get("/openapi.json")
    assert fetches["n"] == after_first  # served from cache
    healthy_client.get("/openapi.json", params={"refresh": "1"})
    assert fetches["n"] > after_first  # escape hatch refetches


def test_swagger_ui_served_at_the_gateway(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "SmartFoodOps API" in r.text
