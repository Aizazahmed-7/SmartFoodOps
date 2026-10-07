"""The Temporal worker for RefundNotificationWorkflow (ADR-0040).

Shares notification_db with the API process (order's worker does the same)
and runs no Kafka consumers and no Celery — those belong to the other two
processes. It exists so the refund lookup can retry for an hour without
anything holding a Kafka partition open.

build_worker() is the covered seam; main() is live wiring, exercised by the
compose stack rather than the unit suite.
"""

import asyncio

import httpx
from smartfood_otel import get_logger, setup_logging, setup_tracing
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from .activities import NotificationActivities
from .config import Settings
from .values import TASK_QUEUE_DEFAULT
from .workflows import RefundNotificationWorkflow

log = get_logger("notification.worker")


def build_worker(
    client: Client, activities: NotificationActivities, *, task_queue: str = TASK_QUEUE_DEFAULT
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[RefundNotificationWorkflow],
        activities=activities.all(),
    )


async def main() -> None:  # pragma: no cover — live wiring (compose runs it)
    settings = Settings()
    setup_logging("notification-worker")
    setup_tracing("notification-worker", settings.otlp_endpoint)

    if settings.redis_url:
        # The worker writes bell rows of its own, so it needs its own
        # publisher — push's module-level one is per PROCESS, and the API's
        # wiring does not reach here.
        import redis.asyncio as aioredis
        from smartfood_realtime import RedisRealtime

        from . import push

        push.set_publisher(RedisRealtime(aioredis.from_url(settings.redis_url)))

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    http = httpx.AsyncClient(timeout=5.0)
    activities = NotificationActivities(sessions, http, order_base_url=settings.order_base_url)

    client: Client | None = None
    while client is None:  # temporal may still be booting — retry forever
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
        except Exception as exc:  # noqa: BLE001 — any connect failure is retryable
            log.warning("temporal not ready — retrying", error=str(exc))
            await asyncio.sleep(2)

    log.info("notification worker started", task_queue=settings.notification_task_queue)
    await build_worker(client, activities, task_queue=settings.notification_task_queue).run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
