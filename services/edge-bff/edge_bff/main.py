"""edge-bff — the front door (ADR-0003, ADR-0005).

Verify the JWT once, strip anything the client claimed about itself, stamp
verified identity headers, forward to the owning service. No business logic,
no database — deliberately thin so it scales as N identical tasks.
"""

import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import jwt as pyjwt
import structlog
from fastapi import FastAPI, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from smartfood_api import ApiError, ErrorCode, install_error_handlers, mount_observability
from smartfood_auth import STRIP_HEADERS, JwksVerifier, context_from_claims, headers_for
from smartfood_otel import RequestContextMiddleware, get_logger, setup_logging, setup_tracing

from .config import Settings
from .limiter import RateLimiter
from .openapi import merge_specs
from .routing import RULES, limit_class_for, match, needs_auth, resolve

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
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("edge-bff")
    setup_tracing("edge-bff", settings.otlp_endpoint)

    http_client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.proxy_timeout_seconds, connect=3.0)
    )
    token_verifier = verifier or JwksVerifier(
        settings.identity_jwks_url,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        cache_ttl=settings.jwks_cache_ttl,
    )
    if limiter is None:
        import redis.asyncio as aioredis

        limiter = RateLimiter(
            aioredis.from_url(settings.redis_url) if settings.redis_url else None,
            limits={
                "auth": settings.rate_limit_auth_per_window,
                "read": settings.rate_limit_read_per_window,
                "write": settings.rate_limit_write_per_window,
            },
            window_seconds=settings.rate_limit_window_seconds,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await http_client.aclose()
        await limiter.aclose()

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
    mount_observability(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "edge-bff"}

    # ── the FE's single API contract (edge_bff/openapi.py) ─────────

    resolved_rules = resolve(RULES, settings)  # bind names -> URLs, once
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
        rule = match("/" + path, resolved_rules)
        if rule is None:
            raise ApiError(ErrorCode.NOT_FOUND, "no such route", 404)

        # 1. Authenticate when the route demands it.
        forwarded = request.headers.get("x-forwarded-for", "")
        client_ip = forwarded.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        scope = f"ip:{client_ip}"  # overridden with the verified sub below
        stamped: dict[str, str] = {}
        if needs_auth(rule, request.method):
            auth_header = request.headers.get("authorization", "")
            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                raise ApiError(
                    ErrorCode.AUTH_INVALID_CREDENTIALS,
                    "missing bearer token",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                claims = await token_verifier.verify(token)
            except pyjwt.ExpiredSignatureError:
                raise ApiError(
                    ErrorCode.AUTH_TOKEN_EXPIRED,
                    "access token expired — refresh",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                ) from None
            except pyjwt.InvalidTokenError:
                raise ApiError(
                    ErrorCode.AUTH_INVALID_CREDENTIALS,
                    "invalid token",
                    401,
                    headers={"WWW-Authenticate": "Bearer"},
                ) from None
            stamped = headers_for(context_from_claims(claims))
            scope = f"sub:{claims.get('sub', 'unknown')}"

        # 1b. Rate limit — AFTER auth on purpose: an authed caller is
        # limited by who they are (one NAT full of customers must not share
        # a bucket), an anonymous one by where they came from. Trusting the
        # first X-Forwarded-For hop is fine HERE because the gateway in
        # front of us sets it; expose this edge directly and that hop
        # becomes client-controlled — a deploy-topology invariant, noted in
        # the runbook.
        decision = await limiter.check(limit_class_for(rule, request.method), scope)
        if decision is not None and not decision.allowed:
            raise ApiError(
                ErrorCode.RATE_LIMITED,
                "too many requests — slow down",
                429,
                headers={
                    "Retry-After": str(max(1, decision.reset_epoch - int(time.time()))),
                    "RateLimit-Limit": str(decision.limit),
                    "RateLimit-Remaining": str(decision.remaining),
                    "RateLimit-Reset": str(decision.reset_epoch),
                },
            )

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
        url = rule.base_url + "/" + path
        try:
            upstream = await http_client.request(
                request.method,
                url,
                params=request.url.query,
                content=await request.body(),
                headers=forward,
            )
        except httpx.HTTPError:
            log.warning("upstream unavailable", upstream=rule.base_url, path=path)
            raise ApiError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
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
