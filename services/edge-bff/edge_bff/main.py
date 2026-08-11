"""edge-bff — the front door (ADR-0003, ADR-0005).

Verify the JWT once, strip anything the client claimed about itself, stamp
verified identity headers, forward to the owning service. No business logic,
no database — deliberately thin so it scales as N identical tasks.
"""

from contextlib import asynccontextmanager
from typing import Any

import httpx
import jwt as pyjwt
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from smartfood_api import ApiError, install_error_handlers
from smartfood_auth import STRIP_HEADERS, JwksVerifier, context_from_claims, headers_for
from smartfood_otel import RequestContextMiddleware, get_logger, setup_logging

from .config import Settings
from .openapi import merge_specs
from .routing import RULES, match, needs_auth

log = get_logger("edge-bff")

# Never forwarded upstream (identity headers are re-stamped after verify;
# the rest are hop-by-hop or recomputed by httpx).
_DROP_INBOUND = {h.lower() for h in STRIP_HEADERS} | {
    "host",
    "content-length",
    "connection",
    "authorization",
}
_DROP_OUTBOUND = {"content-length", "transfer-encoding", "connection", "content-encoding"}


def create_app(
    settings: Settings | None = None,
    *,
    http: httpx.AsyncClient | None = None,
    verifier: JwksVerifier | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("edge-bff")

    http_client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.proxy_timeout_seconds, connect=3.0)
    )
    token_verifier = verifier or JwksVerifier(
        settings.identity_jwks_url,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        cache_ttl=settings.jwks_cache_ttl,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await http_client.aclose()

    # Default docs disabled: they would describe the edge itself (a catch-all
    # proxy — useless). /docs + /openapi.json below serve the MERGED spec.
    app = FastAPI(
        title="edge-bff",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "edge-bff"}

    # ── the FE's single API contract (edge_bff/openapi.py) ─────────

    upstreams = sorted({rule.upstream for rule in RULES})  # allowlist = sources

    async def _build_spec() -> dict[str, Any]:
        specs: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for upstream in upstreams:
            try:
                resp = await http_client.get(getattr(settings, upstream) + "/openapi.json")
                resp.raise_for_status()
                specs[upstream] = resp.json()
            except (httpx.HTTPError, ValueError):
                missing.append(upstream)  # partial docs beat no docs
        return merge_specs(specs, missing)

    @app.get("/openapi.json")
    async def merged_openapi(request: Request) -> dict[str, Any]:
        cached = getattr(app.state, "merged_spec", None)
        if cached is None or "refresh" in request.query_params:
            spec = await _build_spec()
            # Cache only a COMPLETE doc — a partial one (some upstream down
            # at fetch time) rebuilds per request until everyone is back.
            if "x-unavailable-upstreams" not in spec:
                app.state.merged_spec = spec
            return spec
        return cached

    @app.get("/docs")
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="SmartFoodOps API")

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    )
    async def proxy(path: str, request: Request) -> Response:
        rule = match("/" + path)
        if rule is None:
            raise ApiError("NOT_FOUND", "no such route", 404)

        # 1. Authenticate when the route demands it.
        stamped: dict[str, str] = {}
        if needs_auth(rule, request.method):
            auth_header = request.headers.get("authorization", "")
            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise ApiError(
                    "AUTH_INVALID_CREDENTIALS",
                    "missing bearer token",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                claims = await token_verifier.verify(token)
            except pyjwt.ExpiredSignatureError:
                raise ApiError(
                    "AUTH_TOKEN_EXPIRED",
                    "access token expired — refresh",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                ) from None
            except pyjwt.InvalidTokenError:
                raise ApiError(
                    "AUTH_INVALID_CREDENTIALS",
                    "invalid token",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                ) from None
            stamped = headers_for(context_from_claims(claims))

        # 2. Build forward headers: inbound minus identity/hop-by-hop, plus stamped.
        #    Client-supplied X-Auth-* dies here, verified or not (ADR-0005).
        forward = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_INBOUND}
        forward.update(stamped)
        ctx = structlog.contextvars.get_contextvars()
        if request_id := ctx.get("request_id"):
            forward["x-request-id"] = request_id
        if traceparent := request.scope.get("state", {}).get("traceparent"):
            forward["traceparent"] = traceparent

        # 3. Forward and relay the response.
        upstream_base = getattr(settings, rule.upstream)
        url = upstream_base + "/" + path
        try:
            upstream = await http_client.request(
                request.method,
                url,
                params=request.url.query,
                content=await request.body(),
                headers=forward,
            )
        except httpx.HTTPError:
            log.warning("upstream unavailable", upstream=rule.upstream, path=path)
            raise ApiError(
                "DEPENDENCY_UNAVAILABLE",
                "upstream unavailable",
                503,
                headers={"Retry-After": "1"},
            ) from None

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_OUTBOUND},
        )

    return app


app = create_app()
