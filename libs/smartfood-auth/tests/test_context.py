from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from smartfood_auth import Auth, AuthContext, headers_for, require_role

app = FastAPI()


@app.get("/me")
async def me(ctx: Auth) -> dict:
    return {"sub": ctx.sub, "role": ctx.role}


@app.get("/admin-only")
async def admin_only(
    ctx: Annotated[AuthContext, Depends(require_role("restaurant_admin"))],
) -> dict:
    return {"restaurant_id": ctx.restaurant_id}


client = TestClient(app)


def test_missing_headers_is_401():
    assert client.get("/me").status_code == 401


def test_invalid_role_is_401():
    r = client.get("/me", headers={"X-Auth-Sub": "u1", "X-Auth-Role": "superuser"})
    assert r.status_code == 401


def test_headers_become_context():
    r = client.get("/me", headers={"X-Auth-Sub": "u1", "X-Auth-Role": "customer"})
    assert r.status_code == 200
    assert r.json() == {"sub": "u1", "role": "customer"}


def test_wrong_role_is_403():
    r = client.get("/admin-only", headers={"X-Auth-Sub": "u1", "X-Auth-Role": "customer"})
    assert r.status_code == 403


def test_scoped_role_passes_with_claim():
    ctx = AuthContext(sub="u2", role="restaurant_admin", restaurant_id="rest_1")
    r = client.get("/admin-only", headers=headers_for(ctx))
    assert r.status_code == 200
    assert r.json() == {"restaurant_id": "rest_1"}


def test_system_bypasses_role_gate():
    r = client.get(
        "/admin-only", headers={"X-Auth-Sub": "svc:order-worker", "X-Auth-Role": "system"}
    )
    assert r.status_code == 200
