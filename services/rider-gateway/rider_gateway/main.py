"""rider-gateway — the riders' connection plane (ADR-0006).

ONE WebSocket per online rider: GPS pings come UP (→ Redis GEO + the
liveness keys, every Nth → Kafka), offers and revokes go DOWN (relayed
from dispatch's per-rider Redis channel). The socket is an ACCELERATOR,
never the floor: everything pushed here is also pollable at
GET /v1/rider/me, so a dead socket costs latency, not correctness.

Auth (ADR-0006): the browser's WebSocket API can send subprotocols where
EventSource could not send anything — so the JWT rides
`Sec-WebSocket-Protocol: bearer,<jwt>` and the gateway (an edge-class
component) verifies it against JWKS itself. Never query strings. The
connection is then BOUND to the verified rider_id: GPS frames are
attributed from connection state, never from payload (a rider cannot
spoof another rider's position by lying in a frame).

Frames are JSON tonight; the ~30-byte binary protobuf of the ADR is a
named encoding seam — three sim riders don't need it, thirty thousand do.
"""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from typing import Any

import jwt as pyjwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from smartfood_auth import JwksVerifier, Role
from smartfood_otel import RequestContextMiddleware, get_logger, setup_logging, setup_tracing
from smartfood_realtime import RedisRealtime

from .config import Settings
from .ingest import LocationIngest

log = get_logger("rider-gateway")

# WS close codes: 4401 = our "unauthorized" in the app range (1000-3999
# are reserved/registered; 4xxx is application-defined by RFC 6455).
WS_UNAUTHORIZED = 4401


def rider_channel(rider_id: str) -> str:
    """MUST match dispatch/domain/service.py's spelling — the two services
    may not import each other; drift = offers stop arriving, loudly."""
    return f"sfo:rider:{rider_id}"


def create_app(
    settings: Settings | None = None,
    *,
    verifier: JwksVerifier | None = None,
    ingest: LocationIngest | None = None,
    realtime: RedisRealtime | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("rider-gateway")
    setup_tracing("rider-gateway", settings.otlp_endpoint)

    token_verifier = verifier or JwksVerifier(
        settings.identity_jwks_url,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        cache_ttl=settings.jwks_cache_ttl,
    )

    own_redis = None
    own_producer = None
    if (ingest is None or realtime is None) and settings.redis_url:  # pragma: no cover — live
        import redis.asyncio as aioredis
        from smartfood_kafka import AvroSerde, EventProducer, SchemaRegistry, Topic, topic

        own_redis = aioredis.from_url(settings.redis_url)
        producer = None
        if settings.rider_locations == "on":
            own_producer = EventProducer(
                settings.kafka_bootstrap, AvroSerde(SchemaRegistry(settings.schema_registry_url))
            )
            producer = own_producer
        ingest = ingest or LocationIngest(
            own_redis,
            cell=settings.cell_id,
            producer=producer,
            topic=topic(settings.cell_id, Topic.RIDER_LOCATIONS),
            sample_every=settings.location_sample_every,
        )
        realtime = realtime or RedisRealtime(own_redis)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if own_producer is not None:  # pragma: no cover — live wiring
            await own_producer.start()
        yield
        if own_producer is not None:  # pragma: no cover — live wiring
            await own_producer.stop()
        if own_redis is not None:  # pragma: no cover — live wiring
            await own_redis.aclose()

    app = FastAPI(title="rider-gateway", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "rider-gateway"}

    async def _authenticate(websocket: WebSocket) -> str | None:
        """The subprotocol handshake: 'bearer,<jwt>' in, rider_id out —
        or a 4401 close and None. Verification happens BEFORE accept()."""
        offered = websocket.headers.get("sec-websocket-protocol", "")
        parts = [part.strip() for part in offered.split(",")]
        if len(parts) != 2 or parts[0] != "bearer" or not parts[1]:
            await websocket.close(code=WS_UNAUTHORIZED)
            return None
        try:
            claims = await token_verifier.verify(parts[1])
        except pyjwt.InvalidTokenError:
            await websocket.close(code=WS_UNAUTHORIZED)
            return None
        if claims.get("role") != Role.RIDER:
            await websocket.close(code=WS_UNAUTHORIZED)
            return None
        return str(claims.get("rider_id") or claims["sub"])

    @app.websocket("/ws/rider")
    async def rider_socket(websocket: WebSocket) -> None:
        rider_id = await _authenticate(websocket)
        if rider_id is None:
            return
        # Echo the FIRST offered subprotocol ("bearer") — the token half
        # must never be echoed back into response headers.
        await websocket.accept(subprotocol="bearer")
        log.info("rider connected", rider_id=rider_id)

        async def relay() -> None:
            """Dispatch's per-rider channel → the socket, frames verbatim.
            (Requires the realtime bus; without redis the socket is
            ingest-less and push-less — REST keeps the rider whole.)"""
            if realtime is None:
                await asyncio.Event().wait()  # park forever; receiver drives the session
                return  # pragma: no cover — unreachable; narrows the type below
            async with realtime.subscription(rider_channel(rider_id)) as subscription:
                while True:
                    message = await subscription.next_message()
                    if message is not None:
                        await websocket.send_text(message)

        async def receive() -> None:
            count = 0
            while True:
                raw = await websocket.receive_text()
                try:
                    frame: dict[str, Any] = json.loads(raw)
                except ValueError:
                    continue  # malformed frames are dropped, never fatal
                if frame.get("type") != "ping":
                    continue
                lat, lon = frame.get("lat"), frame.get("lon")
                if not isinstance(lat, int | float) or not isinstance(lon, int | float):
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                count += 1
                if ingest is not None:
                    # Attribution from CONNECTION state (rider_id), never
                    # from the frame — the binding auth bought us.
                    await ingest.ping(rider_id, float(lat), float(lon), count=count)

        relay_task = asyncio.create_task(relay())
        try:
            await receive()
        except WebSocketDisconnect:
            pass
        finally:
            relay_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await relay_task
            # No geo cleanup here ON PURPOSE: the 90s heartbeat TTL is the
            # liveness truth (FR-32) — a reconnecting rider (elevator,
            # tunnel) keeps their pin; a truly gone one expires.
            log.info("rider disconnected", rider_id=rider_id)

    return app


app = create_app()
