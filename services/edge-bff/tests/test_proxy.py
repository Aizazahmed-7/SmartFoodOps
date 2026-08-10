import httpx
import pytest
from edge_bff.config import Settings
from edge_bff.main import create_app
from fastapi.testclient import TestClient
from smartfood_auth import JwksVerifier, TokenIssuer, generate_rsa_key, jwks

ISS = "http://identity.test"
AUD = "sfo-api"

KEY = generate_rsa_key()
ISSUER = TokenIssuer(KEY, issuer=ISS, audience=AUD)


class Upstream:
    """Records every request the BFF forwards; answers with a canned response."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "identity.test" and request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json=jwks([KEY]))
        if request.url.host == "down.test":
            raise httpx.ConnectError("boom")
        self.requests.append(request)
        return httpx.Response(
            201, json={"from": request.url.host}, headers={"X-Upstream": "yes"}
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture()
def upstream():
    return Upstream()


@pytest.fixture()
def client(upstream):
    settings = Settings(
        identity_base_url="http://identity.svc",
        catalog_base_url="http://catalog.svc",
        order_base_url="http://down.test",
        identity_jwks_url="http://identity.test/.well-known/jwks.json",
        token_issuer=ISS,
    )
    transport = httpx.MockTransport(upstream.handler)
    app = create_app(
        settings,
        http=httpx.AsyncClient(transport=transport),
        verifier=JwksVerifier(
            settings.identity_jwks_url,
            issuer=ISS,
            audience=AUD,
            http=httpx.AsyncClient(transport=transport),
        ),
    )
    with TestClient(app) as c:
        yield c


def bearer(role="customer", **kw) -> dict:
    return {"Authorization": f"Bearer {ISSUER.issue(sub='usr_1', role=role, **kw)}"}


def test_public_route_forwards_without_token(client, upstream):
    r = client.post("/v1/auth/login", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 201
    assert upstream.last.url == "http://identity.svc/v1/auth/login"


def test_spoofed_identity_headers_are_stripped(client, upstream):
    client.post(
        "/v1/auth/login",
        json={},
        headers={"X-Auth-Sub": "usr_evil", "X-Auth-Role": "system_admin"},
    )
    assert "x-auth-sub" not in upstream.last.headers
    assert "x-auth-role" not in upstream.last.headers


def test_auth_route_without_token_is_401(client):
    r = client.get("/v1/auth/me")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_garbage_token_is_401(client):
    assert client.get("/v1/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_valid_token_becomes_stamped_headers(client, upstream):
    r = client.get("/v1/auth/me", headers=bearer())
    assert r.status_code == 201
    fwd = upstream.last.headers
    assert fwd["x-auth-sub"] == "usr_1"
    assert fwd["x-auth-role"] == "customer"
    assert "authorization" not in fwd  # token is not the services' business


def test_scoping_claims_forwarded(client, upstream):
    client.get("/v1/auth/me", headers=bearer(role="restaurant_admin", restaurant_id="rest_7"))
    assert upstream.last.headers["x-auth-restaurant-id"] == "rest_7"


def test_spoof_plus_valid_token_still_uses_claims(client, upstream):
    client.get(
        "/v1/auth/me",
        headers={**bearer(), "X-Auth-Sub": "usr_evil", "X-Auth-Role": "system_admin"},
    )
    assert upstream.last.headers["x-auth-sub"] == "usr_1"
    assert upstream.last.headers["x-auth-role"] == "customer"


def test_public_read_get_is_anonymous_but_write_needs_token(client, upstream):
    assert client.get("/v1/menus/rest_1").status_code == 201
    assert client.post("/v1/menus", json={}).status_code == 401
    assert client.post("/v1/menus", json={}, headers=bearer()).status_code == 201


def test_query_string_and_request_id_forwarded(client, upstream):
    client.get("/v1/restaurants?city=SPR&page=2")
    assert str(upstream.last.url) == "http://catalog.svc/v1/restaurants?city=SPR&page=2"
    assert upstream.last.headers.get("x-request-id")
    assert upstream.last.headers.get("traceparent")


def test_unknown_path_is_404(client):
    r = client.get("/v1/nonsense")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_expired_token_gets_distinct_code(client):
    expired = TokenIssuer(KEY, issuer=ISS, audience=AUD, ttl_seconds=-10).issue(
        sub="u1", role="customer"
    )
    r = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_upstream_down_is_503_with_retry_after(client):
    r = client.post("/v1/orders", json={}, headers=bearer())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert r.headers["Retry-After"] == "1"


def test_upstream_response_passes_through(client, upstream):
    r = client.get("/v1/restaurants")
    assert r.status_code == 201
    assert r.json() == {"from": "catalog.svc"}
    assert r.headers["x-upstream"] == "yes"
