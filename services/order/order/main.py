"""Order service — the state-machine owner (docs/service-ownership.md).

S2 scope: app factory + the stateless quote. The database, placement,
and the saga wiring land in S3/S5 — this file grows with them."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from smartfood_api import install_error_handlers
from smartfood_otel import RequestContextMiddleware, setup_logging
from smartfood_pricing import PricingConfig

from .adapters.catalog_client import CatalogClient
from .api.routes import router
from .config import Settings
from .domain.ports import CatalogPort
from .domain.service import OrderService


def create_app(
    settings: Settings | None = None,
    *,
    catalog: CatalogPort | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("order")

    # DI seam: tests inject a fake catalog; production builds the real
    # client and owns its http lifecycle in the lifespan.
    own_http: httpx.AsyncClient | None = None
    if catalog is None:
        own_http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0))
        catalog = CatalogClient(settings.catalog_base_url, own_http)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if own_http is not None:
            await own_http.aclose()

    app = FastAPI(title="order", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.state.service = OrderService(
        catalog,
        pricing=PricingConfig(
            delivery_fee_cents=settings.delivery_fee_cents,
            tax_basis_points=settings.tax_basis_points,
        ),
    )
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "order"}

    return app


app = create_app()
