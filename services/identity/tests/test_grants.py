"""Every branch of the internal grant endpoint and its domain method."""

import jwt as pyjwt
import pytest
from identity.config import Settings
from smartfood_auth import AuthContext, headers_for

SYSTEM = headers_for(AuthContext(sub="svc:catalog", role="system"))
REG = {"email": "owner@example.com", "password": "hunter2hunter2"}


def _claims(client) -> dict:
    token = client.post("/v1/auth/login", json=REG).json()["access_token"]
    return pyjwt.decode(token, options={"verify_signature": False}, algorithms=["RS256"])


def _register(client) -> str:
    client.post("/v1/auth/register", json=REG)
    return _claims(client)["sub"]


def _grant(client, user_id: str, restaurant_id: str = "rst_1", headers: dict = SYSTEM):
    return client.post(
        "/v1/internal/grants",
        json={"user_id": user_id, "role": "restaurant_admin", "restaurant_id": restaurant_id},
        headers=headers,
    )


def test_grant_promotes_customer(client):
    user_id = _register(client)
    assert _grant(client, user_id).status_code == 200
    claims = _claims(client)  # fresh login sees the new role
    assert claims["role"] == "restaurant_admin"
    assert claims["restaurant_id"] == "rst_1"


def test_grant_replay_is_silent_success(client):
    user_id = _register(client)
    assert _grant(client, user_id).status_code == 200
    assert _grant(client, user_id).status_code == 200  # idempotent repair path


def test_grant_conflicts_with_other_restaurant(client):
    user_id = _register(client)
    _grant(client, user_id, "rst_1")
    r = _grant(client, user_id, "rst_2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "GRANT_CONFLICT"


def test_grant_unknown_user_is_404(client):
    assert _grant(client, "usr_ghost").status_code == 404


def test_grant_requires_system_role(client):
    user_id = _register(client)
    customer = headers_for(AuthContext(sub=user_id, role="customer"))
    assert _grant(client, user_id, headers=customer).status_code == 403
    assert _grant(client, user_id, headers={}).status_code == 401


def test_grant_rejects_bad_payloads(client):
    bad_role = client.post(
        "/v1/internal/grants",
        json={"user_id": "u", "role": "rider", "restaurant_id": "r"},
        headers=SYSTEM,
    )
    assert bad_role.status_code == 422  # only restaurant_admin is grantable
    unknown_field = client.post(
        "/v1/internal/grants",
        json={"user_id": "u", "role": "restaurant_admin", "restaurant_id": "r", "x": 1},
        headers=SYSTEM,
    )
    assert unknown_field.status_code == 422


def test_refresh_carries_the_promotion(client):
    """The rotating refresh token picks up new claims without a re-login —
    the user's next token refresh completes their onboarding."""
    user_id = _register(client)
    pair = client.post("/v1/auth/login", json=REG).json()
    _grant(client, user_id)
    refreshed = client.post(
        "/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    ).json()
    claims = pyjwt.decode(
        refreshed["access_token"], options={"verify_signature": False}, algorithms=["RS256"]
    )
    assert claims["role"] == "restaurant_admin"
    assert claims["restaurant_id"] == "rst_1"


async def test_grant_to_non_customer_role_conflicts(tmp_path):
    """Riders (and admins) can't own restaurants — domain-level, since no
    HTTP path can produce a rider yet."""
    import sqlalchemy as sa
    from identity.db import metadata, users
    from identity.domain.service import GrantConflict, IdentityService
    from identity.keys import load_or_generate
    from smartfood_auth import TokenIssuer
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    settings = Settings(
        database_url="sqlite+aiosqlite://",
        signing_key_path=str(tmp_path / "key.pem"),
        token_issuer="http://identity.test",
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
    svc = IdentityService(
        sessions,
        issuer,
        access_ttl_seconds=settings.access_ttl_seconds,
        refresh_ttl_days=settings.refresh_ttl_days,
    )

    await svc.register(email=REG["email"], password=REG["password"], full_name=None)
    async with sessions() as s:
        user_id = (await s.execute(sa.select(users.c.id))).scalar_one()
        await s.execute(users.update().values(role="rider", rider_id="rid_1"))
        await s.commit()

    with pytest.raises(GrantConflict):
        await svc.grant_restaurant_admin(user_id=user_id, restaurant_id="rst_1")
