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
    return {"sub": ctx.sub, "roles": sorted(ctx.roles)}


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
    r = client.get("/me", headers={"X-Auth-Sub": "u1", "X-Auth-Roles": "superuser"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


def test_headers_become_context():
    r = client.get("/me", headers={"X-Auth-Sub": "u1", "X-Auth-Roles": "customer"})
    assert r.status_code == 200
    assert r.json() == {"sub": "u1", "roles": ["customer"]}


def test_wrong_role_is_403():
    r = client.get("/admin-only", headers={"X-Auth-Sub": "u1", "X-Auth-Roles": "customer"})
    assert r.status_code == 403


def test_scoped_role_passes_with_claim():
    ctx = AuthContext(sub="u2", roles=frozenset({"restaurant_admin"}), restaurant_id="rest_1")
    r = client.get("/admin-only", headers=headers_for(ctx))
    assert r.status_code == 200
    assert r.json() == {"restaurant_id": "rest_1"}


def test_rider_claim_is_stamped():
    ctx = AuthContext(sub="u3", roles=frozenset({"rider"}), rider_id="rid_7")
    assert headers_for(ctx)["X-Auth-Rider-Id"] == "rid_7"


def test_system_bypasses_role_gate():
    r = client.get(
        "/admin-only", headers={"X-Auth-Sub": "svc:order-worker", "X-Auth-Roles": "system"}
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


# ── multi-role (review 2026-09-10) ─────────────────────────────────


def test_x_auth_roles_carries_the_whole_set():
    """The stamped header is the set; `role` is derived for legacy readers."""
    r = client.get(
        "/me",
        headers={"X-Auth-Sub": "u1", "X-Auth-Roles": "customer,restaurant_admin"},
    )
    assert r.status_code == 200
    # Highest privilege held, matching the pre-multi-role single value.
    assert r.json() == {"sub": "u1", "roles": ["customer", "restaurant_admin"]}


def test_a_promoted_owner_passes_a_customer_gate():
    """THE bug this change exists to kill: an owner orders dinner too, and
    CLAUDE.md records the single-role model causing it to recur at every new
    customer-facing endpoint."""

    @app.get("/customers-only")
    async def customers_only(
        ctx: Annotated[AuthContext, Depends(require_role(Role.CUSTOMER))],
    ) -> dict:
        return {"sub": ctx.sub}

    r = TestClient(app).get(
        "/customers-only",
        headers={"X-Auth-Sub": "u1", "X-Auth-Roles": "customer,restaurant_admin"},
    )
    assert r.status_code == 200


def test_one_unknown_role_rejects_the_whole_set():
    """Same strictness as the single-role check it replaces: an unknown role
    is not identity, rather than being silently filtered out."""
    r = client.get("/me", headers={"X-Auth-Sub": "u1", "X-Auth-Roles": "customer,superuser"})
    assert r.status_code == 401


def test_the_new_trusted_header_is_stripped():
    """H_ROLES is trusted downstream, so the edge MUST strip a client's copy —
    a trusted header missing from STRIP_HEADERS is a privilege escalation."""
    from smartfood_auth import STRIP_HEADERS

    assert "X-Auth-Roles" in STRIP_HEADERS
    assert "X-Auth-Role" in STRIP_HEADERS


def test_context_requires_a_non_empty_role_set():
    """A context with no role can pass no gate, so it is not an identity."""
    import pytest

    with pytest.raises(ValueError):
        AuthContext(sub="u1", roles=frozenset())


def test_headers_for_stamps_the_sorted_set():
    from smartfood_auth import headers_for

    stamped = headers_for(AuthContext(sub="u1", roles=frozenset({"rider", "customer"})))
    assert stamped["X-Auth-Roles"] == "customer,rider"  # sorted, comma-joined
    # The legacy single-role header is no longer stamped at all.
    assert "X-Auth-Role" not in stamped


def test_claims_become_the_role_set():
    from smartfood_auth import context_from_claims

    ctx = context_from_claims({"sub": "u1", "roles": ["customer", "rider"]})
    assert ctx.roles == frozenset({"customer", "rider"})
