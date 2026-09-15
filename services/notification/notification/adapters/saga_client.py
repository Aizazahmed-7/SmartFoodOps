"""Starts RefundNotificationWorkflow BY STRING NAME (ADR-0040).

The consumer process must never import workflows.py — that keeps it out of
the Temporal sandbox's import rules, the same split order makes between its
API and its worker.

Workflow id = `ntf::refund::{event_id}`. The event id is already unique per
emitted fact, so a Kafka redelivery addresses the SAME workflow and
REJECT_DUPLICATE turns the second start into a no-op rather than a second
bell entry. That is the property the deterministic notification id used to
carry alone.
"""

import asyncio
from typing import Any, Protocol

from smartfood_otel import get_logger
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from ..values import TASK_QUEUE_DEFAULT, WORKFLOW_REFUND_NOTIFICATION, RefundNotice

log = get_logger("notification.saga")


class RefundNotifierPort(Protocol):
    async def notify_refund(self, notice: RefundNotice) -> None: ...


class TemporalRefundNotifier:
    def __init__(
        self,
        address: str,
        *,
        namespace: str = "default",
        task_queue: str = TASK_QUEUE_DEFAULT,
        lookup_timeout_seconds: float = 3600.0,
    ):
        self._address = address
        self._namespace = namespace
        self._task_queue = task_queue
        self._lookup_timeout = lookup_timeout_seconds
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> Client:
        """Lazy and cached, like order's saga client: a notification service
        with no refunds in flight never opens a Temporal connection."""
        async with self._lock:
            if self._client is None:
                self._client = await Client.connect(self._address, namespace=self._namespace)
            return self._client

    async def notify_refund(self, notice: RefundNotice) -> None:
        client = await self._connect()
        try:
            await client.start_workflow(
                WORKFLOW_REFUND_NOTIFICATION,
                args=[notice, self._lookup_timeout],
                id=f"ntf::refund::{notice.event_id}",
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            # A redelivery of the same event. The first start owns it.
            log.info("refund notification already started", order_id=notice.order_id)


class DisarmedRefundNotifier:
    """No temporal_address configured. Refund notifications are skipped
    rather than failed — the same shape as an empty celery_broker_url
    disarming receipts, so a dev stack without Temporal still consumes."""

    def __init__(self) -> None:
        self.skipped: list[Any] = []

    async def notify_refund(self, notice: RefundNotice) -> None:
        self.skipped.append(notice.order_id)
        log.info("refund notification skipped — temporal disarmed", order_id=notice.order_id)
