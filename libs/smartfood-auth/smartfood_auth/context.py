"""AuthContext — how every domain service consumes identity.

Services never parse JWTs (ADR-0005). The edge verified the token once and
stamped X-Auth-* headers; the network (private subnets / compose network)
is what makes those headers trustworthy. This module turns them back into
a typed object and provides the role gate.
"""

from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BaseModel, Field
from smartfood_api import ApiError, ErrorCode


class MissingIdentity(ApiError):
    """No/garbage identity headers. An ApiError subclass so the standard
    handlers render the proper envelope (code AUTH_INVALID_CREDENTIALS)
    instead of the bare-HTTPException fallback table smartfood-api's own
    comment discourages."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.AUTH_INVALID_CREDENTIALS, "missing or invalid identity", 401)


class Forbidden(ApiError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.FORBIDDEN_ROLE, "forbidden", 403)


class Role(StrEnum):
    """The closed role vocabulary — route gates name members, never raw
    strings, so a typo'd role is an AttributeError at import time instead
    of a silently-impossible gate."""

    CUSTOMER = "customer"
    RESTAURANT_ADMIN = "restaurant_admin"
    RIDER = "rider"
    SYSTEM_ADMIN = "system_admin"
    SYSTEM = "system"


# Plain strings on purpose: header values compare/hash as str.
ROLES = frozenset(str(role) for role in Role)

H_SUB = "X-Auth-Sub"
H_ROLE = "X-Auth-Role"
H_ROLES = "X-Auth-Roles"
H_RESTAURANT = "X-Auth-Restaurant-Id"
H_RIDER = "X-Auth-Rider-Id"

# Everything the edge must strip from inbound requests before stamping its own.
# These headers are TRUSTED downstream, so a client that could set one would
# be choosing its own identity — adding a trusted header without adding it
# here is the whole attack. H_ROLE is no longer stamped or read, but stays
# listed: during a rolling deploy an un-upgraded service still trusts it.
STRIP_HEADERS = (H_SUB, H_ROLE, H_ROLES, H_RESTAURANT, H_RIDER)


class AuthContext(BaseModel):
    sub: str
    # Required and non-empty: a context with no role can pass no gate, so it
    # is not an identity — better rejected at construction than carried.
    roles: frozenset[str] = Field(min_length=1)
    restaurant_id: str | None = None
    rider_id: str | None = None


async def get_auth_context(request: Request) -> AuthContext:
    sub = request.headers.get(H_SUB)
    stamped = request.headers.get(H_ROLES) or ""
    held = frozenset(part for part in (p.strip() for p in stamped.split(",")) if part)
    # An unknown role is not identity — the strictness the single-role check
    # had, applied to every member of the set.
    if not sub or not held or not held <= ROLES:
        raise MissingIdentity()
    return AuthContext(
        sub=sub,
        roles=held,
        restaurant_id=request.headers.get(H_RESTAURANT),
        rider_id=request.headers.get(H_RIDER),
    )


Auth = Annotated[AuthContext, Depends(get_auth_context)]


def require_role(*roles: Role):
    """Dependency factory: `ctx: AuthContext = Depends(require_role(Role.RESTAURANT_ADMIN))`.

    `system` (Temporal workers, internal jobs) always passes — services must
    not apply user-level ownership checks to system callers (docs §5.2).
    """

    async def dep(ctx: Auth) -> AuthContext:
        allowed = {str(role) for role in roles}
        if str(Role.SYSTEM) not in ctx.roles and not (ctx.roles & allowed):
            raise Forbidden()
        return ctx

    return dep


def require_system():
    """The service-to-service gate: ONLY the system role passes. This is
    require_role() with no roles, given a name that states the intent —
    call sites no longer need a defensive comment explaining the trick."""
    return require_role()
