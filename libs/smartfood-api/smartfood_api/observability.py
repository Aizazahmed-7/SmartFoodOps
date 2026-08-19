"""The ops surface every service mounts: GET /metrics and GET /readyz.

Lives here, not in smartfood-otel, because it couples to FastAPI — the same
reason install_error_handlers does. One call, `mount_observability(app,
engine=engine)`, gives a service Prometheus scraping and a real readiness
probe. Pass engine=None (the edge-bff, which owns no database) and /readyz
degrades to a liveness answer.

Liveness vs readiness, kept distinct on purpose: /healthz (defined per
service) says "the process is up"; /readyz says "and its database answers",
so an orchestrator drains a pod whose DB link is broken instead of routing
into 500s. Both are excluded from the access log and the HTTP histogram.

Routes are registered via add_api_route (not the @app.get decorator) so the
handlers stay named, referenced values — the strict-tier reportUnusedFunction
has nothing to flag, and no per-line ignore is needed.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from smartfood_otel import render_metrics


def mount_observability(app: FastAPI, *, engine: Any | None = None) -> None:
    async def metrics() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    async def readyz() -> Response:
        if engine is not None:
            # A cheap round trip proves the pool can hand out a live
            # connection — the failure mode readiness exists to catch.
            from sqlalchemy import text

            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception:
                return JSONResponse({"status": "not ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.add_api_route("/metrics", metrics, include_in_schema=False)
    app.add_api_route("/readyz", readyz, include_in_schema=False)
