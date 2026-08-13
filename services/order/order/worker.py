"""The Temporal worker process — replaces the S1 keep-alive skeleton.

Runs OrderWorkflow + the activities against the order-tq task queue. This
process shares order_db with the API but deliberately does NOT run the
outbox poller (flag #7: one poller instance per service — the API process
owns it; the worker only stages rows through its activities).

build_worker() is the covered seam (tests hand in the time-skipping
client); main() is live wiring — connect-with-retry, real adapters —
exercised by the compose stack, not the unit suite.
"""

import asyncio

import httpx
from smartfood_otel import get_logger, setup_logging
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from .activities import OrderActivities
from .adapters.inventory_client import InventoryClient
from .adapters.payment_client import PaymentClient
from .config import Settings
from .workflows import DeliveryWorkflow, OrderWorkflow

log = get_logger("order.worker")


def build_worker(client: Client, activities: OrderActivities, *, task_queue: str) -> Worker:
    """The covered composition seam: everything main() wires is injectable."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities.all(),
    )


async def main() -> None:  # pragma: no cover — live wiring (compose runs it)
    settings = Settings()
    setup_logging("order-worker")

    client: Client | None = None
    while client is None:  # temporal may still be booting — retry forever
        try:
            client = await Client.connect(settings.temporal_address)
        except Exception as exc:
            log.warning("temporal not ready — retrying", error=str(exc))
            await asyncio.sleep(2)

    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as http:
        activities = OrderActivities(
            sessions,
            InventoryClient(settings.inventory_base_url, http),
            PaymentClient(settings.payment_base_url, http),
        )
        worker = build_worker(client, activities, task_queue=settings.task_queue)
        log.info("order-worker up", task_queue=settings.task_queue)
        await worker.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
