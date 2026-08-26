import httpx
import pytest
from identity.config import Settings
from smartfood_auth import JwksVerifier, headers_for
from smartfood_auth.stamp import context_from_claims

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
    r = client.patch(
        "/v1/auth/me", json={"full_name": "Ali Khan", "phone": "+92300123"}, headers=headers
    )
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


def test_address_creation_is_capped(client):
    """Creation cap (20) is what makes the unpaginated list endpoint correct
    — the table can't grow unbounded per user (api-standards §pagination)."""
    headers = _login_headers(client)
    body = {"label": "spot", "line1": "1 Main St", "city": "Springfield"}
    for _ in range(20):
        assert client.post("/v1/me/addresses", json=body, headers=headers).status_code == 201
    over = client.post("/v1/me/addresses", json=body, headers=headers)
    assert over.status_code == 422
    assert over.json()["error"]["details"][0]["field"] == "addresses"
    # Deleting one frees a slot — the cap counts live rows, not history.
    address_id = client.get("/v1/me/addresses", headers=headers).json()[0]["id"]
    client.delete(f"/v1/me/addresses/{address_id}", headers=headers)
    assert client.post("/v1/me/addresses", json=body, headers=headers).status_code == 201


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


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "identity"}


def test_refresh_with_unknown_token_is_401(client):
    r = client.post("/v1/auth/refresh", json={"refresh_token": "never-issued"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_me_for_unknown_user_is_404(client):
    from smartfood_auth import AuthContext

    ghost = headers_for(AuthContext(sub="usr_ghost", role="customer"))
    assert client.get("/v1/auth/me", headers=ghost).status_code == 404


def test_empty_profile_update_is_422(client):
    headers = _login_headers(client)
    r = client.patch("/v1/auth/me", json={}, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_profile_update_for_unknown_user_is_404(client):
    from smartfood_auth import AuthContext

    ghost = headers_for(AuthContext(sub="usr_ghost", role="customer"))
    r = client.patch("/v1/auth/me", json={"full_name": "X"}, headers=ghost)
    assert r.status_code == 404


def test_load_or_generate_round_trips_the_same_kid(tmp_path):
    """Generate on first boot, LOAD on every restart — the kid (and thus
    every issued token) must survive the round trip. Hermetic on purpose:
    the load branch used to be covered only via the machine's ambient
    infra/local/keys file (absent in clean CI), which made the coverage
    bar environment-dependent. Both branches are driven HERE."""
    from identity.keys import load_or_generate

    path = tmp_path / "keys" / "identity-rsa.pem"
    born = load_or_generate(str(path))  # generate: file did not exist
    assert path.exists()
    reloaded = load_or_generate(str(path))  # load: same identity back
    assert reloaded.kid == born.kid
    assert reloaded.public_jwk == born.public_jwk
    assert reloaded.private_pem == born.private_pem


def test_non_rsa_key_file_rejected(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from identity.keys import load_or_generate

    path = tmp_path / "ec.pem"
    path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="not an RSA private key"):
        load_or_generate(str(path))


# ── domain-level tests ─────────────────────────────────────────────
# Paths the HTTP surface cannot reach (no delete-user endpoint; TTLs are
# server-side). The layering pays off here: IdentityService runs bare.


async def _domain_service(tmp_path, **overrides):
    from identity.db import metadata
    from identity.domain.service import IdentityService
    from identity.keys import load_or_generate
    from smartfood_auth import TokenIssuer
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    settings = Settings(
        database_url="sqlite+aiosqlite://",
        signing_key_path=str(tmp_path / "key.pem"),
        token_issuer="http://identity.test",
        **overrides,
    )
    engine = create_async_engine(
        settings.database_url, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    key = load_or_generate(settings.signing_key_path)
    issuer = TokenIssuer(
        key,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        ttl_seconds=settings.access_ttl_seconds,
    )
    return IdentityService(
        sessions,
        issuer,
        access_ttl_seconds=settings.access_ttl_seconds,
        refresh_ttl_days=settings.refresh_ttl_days,
    ), sessions


async def test_expired_refresh_token_rejected(tmp_path):
    from identity.domain.service import InvalidRefreshToken

    svc, _ = await _domain_service(tmp_path, refresh_ttl_days=-1)  # tokens born expired
    await svc.register(email=REG["email"], password=REG["password"], full_name=None)
    pair = await svc.login(email=REG["email"], password=REG["password"])
    with pytest.raises(InvalidRefreshToken):
        await svc.refresh(pair.refresh_token)


async def test_refresh_for_vanished_user_rejected(tmp_path):
    """Defensive guard: the refresh row survives but the user row is gone."""
    from identity.db import users
    from identity.domain.service import InvalidRefreshToken

    svc, sessions = await _domain_service(tmp_path)
    await svc.register(email=REG["email"], password=REG["password"], full_name=None)
    pair = await svc.login(email=REG["email"], password=REG["password"])
    async with sessions() as s:
        await s.execute(users.delete())
        await s.commit()
    with pytest.raises(InvalidRefreshToken):
        await svc.refresh(pair.refresh_token)


def test_internal_address_read_for_placement(client):
    """Order placement's server-side address resolution: system-only,
    ownership in the query (foreign/unknown → 404, same shape)."""
    from smartfood_auth import AuthContext

    headers = _login_headers(client)
    created = client.post(
        "/v1/me/addresses",
        json={"label": "home", "line1": "12 Mango St", "city": "Springfield"},
        headers=headers,
    )
    addr_id = created.json()["id"]
    user_id = client.get("/v1/auth/me", headers=headers).json()["id"]

    system = headers_for(AuthContext(sub="svc:order", role="system"))
    r = client.get(f"/v1/internal/users/{user_id}/addresses/{addr_id}", headers=system)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == addr_id
    assert body["line1"] == "12 Mango St"

    # Someone ELSE's user_id with this address id → 404 (ownership in query).
    assert (
        client.get(f"/v1/internal/users/usr_other/addresses/{addr_id}", headers=system).status_code
        == 404
    )
    # Unknown address → 404; non-system caller → 403.
    assert (
        client.get(f"/v1/internal/users/{user_id}/addresses/adr_ghost", headers=system).status_code
        == 404
    )
    assert (
        client.get(f"/v1/internal/users/{user_id}/addresses/{addr_id}", headers=headers).status_code
        == 403
    )


def test_internal_contact_read_for_receipts(client):
    """Notification resolves receipt recipients at send time: system-only,
    narrow contract (id/email/full_name — never role or phone), unknown
    user → 404, non-system caller → 403."""
    from smartfood_auth import AuthContext

    headers = _login_headers(client)
    user_id = client.get("/v1/auth/me", headers=headers).json()["id"]

    system = headers_for(AuthContext(sub="svc:notification-worker", role="system"))
    r = client.get(f"/v1/internal/users/{user_id}", headers=system)
    assert r.status_code == 200
    assert r.json() == {"id": user_id, "email": REG["email"], "full_name": None}

    assert client.get("/v1/internal/users/usr_ghost", headers=system).status_code == 404
    assert client.get(f"/v1/internal/users/{user_id}", headers=headers).status_code == 403
