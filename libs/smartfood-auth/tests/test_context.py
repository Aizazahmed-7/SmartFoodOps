from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from smartfood_api import install_error_handlers
from smartfood_auth import Auth, AuthContext, Role, headers_for, require_role

app = FastAPI()
# The lib's refusals are ApiError subclasses: they render through the
# standard handlers, which every service installs in create_app.
install_error_handlers(app)


@app.get("/me")
async def me(ctx: Auth) -> dict:
    return {"sub": ctx.sub, "role": ctx.role}


@app.get("/admin-only")
async def admin_only(
    ctx: Annotated[AuthContext, Depends(require_role(Role.RESTAURANT_ADMIN))],
) -> dict:
    return {"restaurant_id": ctx.restaurant_id}


client = TestClient(app)


def test_missing_headers_is_401():
    r = client.get("/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_invalid_role_is_401():
    r = client.get("/me", headers={"X-Auth-Sub": "u1", "X-Auth-Role": "superuser"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


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


def test_rider_claim_is_stamped():
    ctx = AuthContext(sub="u3", role="rider", rider_id="rid_7")
    assert headers_for(ctx)["X-Auth-Rider-Id"] == "rid_7"


def test_system_bypasses_role_gate():
    r = client.get(
        "/admin-only", headers={"X-Auth-Sub": "svc:order-worker", "X-Auth-Role": "system"}
    )
    assert r.status_code == 200


def test_auth_refusals_render_the_exact_catalog_codes():
    """The wire contract pin: 401 -> AUTH_INVALID_CREDENTIALS and 403 ->
    FORBIDDEN_ROLE through the lib-owned exceptions, byte-compatible with
    what the fallback table used to produce."""
    from smartfood_auth import Forbidden, MissingIdentity

    missing = MissingIdentity()
    assert (missing.status, str(missing.code)) == (401, "AUTH_INVALID_CREDENTIALS")
    forbidden = Forbidden()
    assert (forbidden.status, str(forbidden.code)) == (403, "FORBIDDEN_ROLE")
