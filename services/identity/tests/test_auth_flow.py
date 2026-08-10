import httpx
import pytest
from fastapi.testclient import TestClient
from identity.config import Settings
from identity.main import create_app
from smartfood_auth import JwksVerifier, headers_for
from smartfood_auth.stamp import context_from_claims


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        create_all=True,
        signing_key_path=str(tmp_path / "key.pem"),
        token_issuer="http://identity.test",
    )
    app = create_app(settings)
    with TestClient(app) as c:  # `with` runs the lifespan (create_all)
        yield c


REG = {"email": "ali@example.com", "password": "hunter2hunter2"}


def test_register_then_login(client):
    assert client.post("/v1/auth/register", json=REG).status_code == 202
    r = client.post("/v1/auth/login", json=REG)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["access_token"] and body["refresh_token"]


def test_duplicate_register_looks_identical(client):
    first = client.post("/v1/auth/register", json=REG)
    second = client.post("/v1/auth/register", json=REG)
    assert (first.status_code, first.json()) == (second.status_code, second.json())


def test_wrong_password_is_uniform_401(client):
    client.post("/v1/auth/register", json=REG)
    r = client.post("/v1/auth/login", json={"email": REG["email"], "password": "wrong"})
    r2 = client.post("/v1/auth/login", json={"email": "ghost@example.com", "password": "x"})
    assert r.status_code == r2.status_code == 401
    # Uniform in everything an attacker can distinguish; request_id differs by design.
    for resp in (r, r2):
        assert resp.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert r.json()["error"]["message"] == r2.json()["error"]["message"]


def test_refresh_rotates(client):
    client.post("/v1/auth/register", json=REG)
    pair = client.post("/v1/auth/login", json=REG).json()

    r = client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["refresh_token"] != pair["refresh_token"]


def test_refresh_reuse_kills_family(client):
    client.post("/v1/auth/register", json=REG)
    pair = client.post("/v1/auth/login", json=REG).json()

    fresh = client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}).json()

    # Attacker replays the ORIGINAL (already-rotated) token…
    reuse = client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert reuse.status_code == 401

    # …and the legitimate NEW token is now dead too — whole family revoked.
    after = client.post("/v1/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert after.status_code == 401


async def test_issued_token_verifies_against_own_jwks(client):
    client.post("/v1/auth/register", json=REG)
    token = client.post("/v1/auth/login", json=REG).json()["access_token"]
    jwks_doc = client.get("/.well-known/jwks.json").json()

    verifier = JwksVerifier(
        "http://identity.test/jwks",
        issuer="http://identity.test",
        audience="sfo-api",
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks_doc))
        ),
    )
    claims = await verifier.verify(token)
    assert claims["role"] == "customer"
    assert claims["sub"].startswith("usr_")


def test_me_via_stamped_headers(client):
    """Simulates the edge: verify would happen there; services get headers."""
    client.post("/v1/auth/register", json=REG)
    token = client.post("/v1/auth/login", json=REG).json()["access_token"]

    import jwt as pyjwt

    claims = pyjwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
    r = client.get("/v1/auth/me", headers=headers_for(context_from_claims(claims)))
    assert r.status_code == 200
    assert r.json()["email"] == REG["email"]
    assert r.json()["role"] == "customer"


def _login_headers(client) -> dict:
    import jwt as pyjwt

    client.post("/v1/auth/register", json=REG)
    token = client.post("/v1/auth/login", json=REG).json()["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])
    return headers_for(context_from_claims(claims))


def test_profile_update(client):
    headers = _login_headers(client)
    r = client.patch("/v1/auth/me", json={"full_name": "Ali Khan", "phone": "+92300123"},
                     headers=headers)
    assert r.status_code == 200
    me = client.get("/v1/auth/me", headers=headers).json()
    assert me["full_name"] == "Ali Khan"
    assert me["phone"] == "+92300123"


def test_address_crud_and_ownership(client):
    headers = _login_headers(client)
    created = client.post(
        "/v1/me/addresses",
        json={"label": "home", "line1": "12 Mango St", "city": "Springfield"},
        headers=headers,
    )
    assert created.status_code == 201
    addr_id = created.json()["id"]

    listed = client.get("/v1/me/addresses", headers=headers).json()
    assert [a["id"] for a in listed] == [addr_id]

    # Another user cannot delete it — ownership is in the query (0 rows → 404).
    from smartfood_auth import AuthContext

    other = headers_for(AuthContext(sub="usr_other", role="customer"))
    assert client.delete(f"/v1/me/addresses/{addr_id}", headers=other).status_code == 404

    assert client.delete(f"/v1/me/addresses/{addr_id}", headers=headers).status_code == 204
    assert client.get("/v1/me/addresses", headers=headers).json() == []


def test_refresh_reuse_returns_distinct_code(client):
    """The legitimate holder learns their token was stolen (AUTH_REFRESH_REUSED)."""
    client.post("/v1/auth/register", json=REG)
    pair = client.post("/v1/auth/login", json=REG).json()
    client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    reuse = client.post("/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "AUTH_REFRESH_REUSED"


def test_unknown_body_field_is_422(client):
    r = client.post("/v1/auth/register", json={**REG, "role": "system_admin"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_short_password_rejected(client):
    r = client.post("/v1/auth/register", json={"email": "x@y.z", "password": "short"})
    assert r.status_code == 422
