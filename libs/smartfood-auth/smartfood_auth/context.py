"""AuthContext — how every domain service consumes identity.

Services never parse JWTs (ADR-0005). The edge verified the token once and
stamped X-Auth-* headers; the network (private subnets / compose network)
is what makes those headers trustworthy. This module turns them back into
a typed object and provides the role gate.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

ROLES = {"customer", "restaurant_admin", "rider", "system_admin", "system"}

H_SUB = "X-Auth-Sub"
H_ROLE = "X-Auth-Role"
H_RESTAURANT = "X-Auth-Restaurant-Id"
H_RIDER = "X-Auth-Rider-Id"

# Everything the edge must strip from inbound requests before stamping its own.
STRIP_HEADERS = (H_SUB, H_ROLE, H_RESTAURANT, H_RIDER)


class AuthContext(BaseModel):
    sub: str
    role: str
    restaurant_id: str | None = None
    rider_id: str | None = None


async def get_auth_context(request: Request) -> AuthContext:
    sub = request.headers.get(H_SUB)
    role = request.headers.get(H_ROLE)
    if not sub or not role or role not in ROLES:
        raise HTTPException(status_code=401, detail="missing or invalid identity")
    return AuthContext(
        sub=sub,
        role=role,
        restaurant_id=request.headers.get(H_RESTAURANT),
        rider_id=request.headers.get(H_RIDER),
    )


Auth = Annotated[AuthContext, Depends(get_auth_context)]


def require_role(*roles: str):
    """Dependency factory: `ctx: AuthContext = Depends(require_role("restaurant_admin"))`.

    `system` (Temporal workers, internal jobs) always passes — services must
    not apply user-level ownership checks to system callers (docs §5.2).
    """

    async def dep(ctx: Auth) -> AuthContext:
        if ctx.role != "system" and ctx.role not in roles:
            raise HTTPException(status_code=403, detail="forbidden")
        return ctx

    return dep
