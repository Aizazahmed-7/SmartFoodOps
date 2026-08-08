"""edge-bff — the front door (ADR-0003, ADR-0005).

Verify the JWT once, strip anything the client claimed about itself, stamp
verified identity headers, forward to the owning service. No business logic,
no database — deliberately thin so it scales as N identical tasks.
"""

from contextlib import asynccontextmanager

import httpx
import jwt as pyjwt
import structlog
from fastapi import FastAPI, Request, Response
from smartfood_auth import STRIP_HEADERS, JwksVerifier, context_from_claims, headers_for
from smartfood_otel import RequestContextMiddleware, get_logger, setup_logging

from .config import Settings
from .routing import match, needs_auth

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

    app = FastAPI(title="edge-bff", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "edge-bff"}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    )
    async def proxy(path: str, request: Request) -> Response:
        rule = match("/" + path)
        if rule is None:
            return Response(
                content=b'{"detail":"not found"}', status_code=404, media_type="application/json"
            )

        # 1. Authenticate when the route demands it.
        stamped: dict[str, str] = {}
        if needs_auth(rule, request.method):
            auth_header = request.headers.get("authorization", "")
            scheme, _, token = auth_header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return Response(
                    content=b'{"detail":"missing bearer token"}',
                    status_code=401,
                    media_type="application/json",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                claims = await token_verifier.verify(token)
            except pyjwt.InvalidTokenError:
                return Response(
                    content=b'{"detail":"invalid token"}',
                    status_code=401,
                    media_type="application/json",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            stamped = headers_for(context_from_claims(claims))

        # 2. Build forward headers: inbound minus identity/hop-by-hop, plus stamped.
        #    Client-supplied X-Auth-* dies here, verified or not (ADR-0005).
        forward = {
            k: v for k, v in request.headers.items() if k.lower() not in _DROP_INBOUND
        }
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
            return Response(
                content=b'{"detail":"upstream unavailable"}',
                status_code=502,
                media_type="application/json",
            )

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_OUTBOUND
            },
        )

    return app


app = create_app()
