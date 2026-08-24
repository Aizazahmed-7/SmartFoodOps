"""Two read surfaces, two audiences (FR-43 ops, FR-55 per-tenant).

The restaurant view scopes by the CLAIM, not a path parameter: there is no
/{restaurant_id} to probe, so cross-tenant reads are unrepresentable — the
ownership check IS the query's WHERE clause (api-standards: not-yours →
404, and here not-yours cannot even be asked for)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from smartfood_api import ApiError, ErrorCode
from smartfood_auth import AuthContext, Role, require_role, require_system

from ..domain.service import AnalyticsService

router = APIRouter()

SystemOnly = Annotated[AuthContext, Depends(require_system())]
RestaurantAdmin = Annotated[AuthContext, Depends(require_role(Role.RESTAURANT_ADMIN))]

Days = Annotated[int, Query(ge=1, le=90)]


def _svc(request: Request) -> AnalyticsService:
    return request.app.state.service


@router.get("/v1/internal/analytics/metrics")
async def ops_metrics(ctx: SystemOnly, request: Request, days: Days = 7) -> dict[str, Any]:
    return await _svc(request).ops_metrics(days)


@router.get("/v1/restaurant/analytics")
async def restaurant_metrics(
    ctx: RestaurantAdmin, request: Request, days: Days = 14
) -> dict[str, Any]:
    if not ctx.restaurant_id:
        # An admin token with no restaurant claim has nothing to be scoped
        # to — same shape as not-found, no existence leaks.
        raise ApiError(ErrorCode.NOT_FOUND, "no restaurant for this account", 404)
    return await _svc(request).restaurant_metrics(ctx.restaurant_id, days)
