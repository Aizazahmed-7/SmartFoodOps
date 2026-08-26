"""Dispatch service — the offer cascade's decision plane (ADR-0011/0026).

The first PG-less service: DynamoDB holds the truth, Redis GEO holds the
candidate index, Kafka gets direct-produced copies. Everything is a port
injected through create_app, so the test app runs on moto + fakes."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from smartfood_api import install_error_handlers, mount_observability
from smartfood_otel import RequestContextMiddleware, setup_logging, setup_tracing

from .api.routes import router
from .config import Settings
from .domain.service import DispatchService


def create_app(
    settings: Settings | None = None,
    *,
    service: DispatchService | None = None,
) -> FastAPI:
    settings = settings or Settings()
    setup_logging("dispatch")
    setup_tracing("dispatch", settings.otlp_endpoint)

    own_ddb = None
    own_redis = None
    own_producer = None
    own_http = None
    if service is None:  # pragma: no cover — live wiring (compose runs it)
        import boto3
        import httpx
        import redis.asyncio as aioredis
        from botocore.config import Config
        from smartfood_kafka import AvroSerde, EventProducer, SchemaRegistry, Topic, topic
        from smartfood_realtime import RedisRealtime

        from .adapters.events import DispatchEvents
        from .adapters.geo import RiderGeo
        from .adapters.order_client import OrderCourierClient
        from .adapters.rider_store import DeliveryStore, RiderStore

        own_ddb = boto3.client(
            "dynamodb",
            endpoint_url=settings.aws_endpoint_url or None,
            region_name=settings.aws_region,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
        own_redis = aioredis.from_url(settings.redis_url) if settings.redis_url else None
        producer = None
        if settings.dispatch_events == "on":
            own_producer = EventProducer(
                settings.kafka_bootstrap, AvroSerde(SchemaRegistry(settings.schema_registry_url))
            )
            producer = own_producer
        own_http = httpx.AsyncClient(timeout=5.0)
        service = DispatchService(
            riders=RiderStore(own_ddb, settings.rider_state_table),
            deliveries=DeliveryStore(own_ddb, settings.deliveries_table),
            geo=RiderGeo(own_redis, cell=settings.cell_id),
            bus=RedisRealtime(own_redis) if own_redis is not None else None,
            courier_events=OrderCourierClient(settings.order_base_url, own_http),
            events=DispatchEvents(
                producer,
                topic=topic(settings.cell_id, Topic.DISPATCH_EVENTS),
                cell_id=settings.cell_id,
            ),
            rider_cap=settings.rider_cap,
            search_radius_km=settings.search_radius_km,
            widened_radius_km=settings.widened_radius_km,
            widen_after_misses=settings.widen_after_misses,
            offer_first_timeout_s=settings.offer_first_timeout_s,
            offer_next_timeout_s=settings.offer_next_timeout_s,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if own_producer is not None:  # pragma: no cover — live wiring
            await own_producer.start()
        if own_ddb is not None and settings.create_tables:  # pragma: no cover — live wiring
            from .adapters.rider_store import ensure_tables

            await asyncio.to_thread(
                ensure_tables,
                own_ddb,
                rider_state=settings.rider_state_table,
                deliveries=settings.deliveries_table,
            )
        yield
        if own_producer is not None:  # pragma: no cover — live wiring
            await own_producer.stop()
        if own_redis is not None:  # pragma: no cover — live wiring
            await own_redis.aclose()
        if own_http is not None:  # pragma: no cover — live wiring
            await own_http.aclose()

    app = FastAPI(title="dispatch", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    mount_observability(app)
    app.state.service = service
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "dispatch"}

    return app


app = create_app()
