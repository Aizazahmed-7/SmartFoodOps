"""Turn a verified identity into forwardable X-Auth-* headers.

Callers: edge-bff after verifying a JWT, and every service-to-service
adapter via internal_headers() — the ADR-0005 internal-trust contract
owned HERE instead of five hand-copies of folklore.
"""

from typing import Any

from smartfood_otel import current_traceparent

from .context import H_RESTAURANT, H_RIDER, H_ROLE, H_SUB, AuthContext


def headers_for(ctx: AuthContext) -> dict[str, str]:
    headers = {H_SUB: ctx.sub, H_ROLE: ctx.role}
    if ctx.restaurant_id:
        headers[H_RESTAURANT] = ctx.restaurant_id
    if ctx.rider_id:
        headers[H_RIDER] = ctx.rider_id
    return headers


def internal_headers(caller: str) -> dict[str, str]:
    """The service-to-service call contract (ADR-0005), in one place:
    system-role X-Auth-* identity (sub=svc:{caller}), X-Internal-Caller,
    and the LIVE traceparent — call this per request, not at import time:
    the trace contextvar changes with every request being served."""
    headers = headers_for(AuthContext(sub=f"svc:{caller}", role="system"))
    headers["X-Internal-Caller"] = caller
    if traceparent := current_traceparent():
        headers["traceparent"] = traceparent
    return headers


def context_from_claims(claims: dict[str, Any]) -> AuthContext:
    """Build the context the edge stamps from verified JWT claims."""
    return AuthContext(
        sub=str(claims["sub"]),
        role=str(claims["role"]),
        restaurant_id=claims.get("restaurant_id"),
        rider_id=claims.get("rider_id"),
    )
