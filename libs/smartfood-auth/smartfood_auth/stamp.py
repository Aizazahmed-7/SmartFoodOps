"""Turn a verified identity into forwardable X-Auth-* headers.

Two callers: edge-bff after verifying a JWT, and (in week 2) Temporal
activities re-stamping the original actor's identity on service calls.
"""

from .context import H_RESTAURANT, H_RIDER, H_ROLE, H_SUB, AuthContext


def headers_for(ctx: AuthContext) -> dict[str, str]:
    headers = {H_SUB: ctx.sub, H_ROLE: ctx.role}
    if ctx.restaurant_id:
        headers[H_RESTAURANT] = ctx.restaurant_id
    if ctx.rider_id:
        headers[H_RIDER] = ctx.rider_id
    return headers


def context_from_claims(claims: dict) -> AuthContext:
    """Build the context the edge stamps from verified JWT claims."""
    return AuthContext(
        sub=claims["sub"],
        role=claims["role"],
        restaurant_id=claims.get("restaurant_id"),
        rider_id=claims.get("rider_id"),
    )
