"""The real SagaPort — replaces S3's logging stub.

Starts OrderWorkflow BY STRING NAME so the API process never imports
workflow code (uvicorn stays sandbox-free; only the worker loads
workflows.py). Workflow id = ord::{order_id} with REJECT_DUPLICATE:
Temporal itself referees duplicate starts — the second start of the same
order attaches nothing and raises AlreadyStarted, which we swallow (the
placement replay path may legitimately call start twice).

The Temporal connection is lazy: created on first use, cached. create_app
stays synchronous and tests that never place orders never connect.
"""

import asyncio

from smartfood_otel import get_logger
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from ..values import WorkflowInput

log = get_logger("order.saga")


class TemporalSaga:
    def __init__(
        self,
        address: str,
        *,
        task_queue: str,
        accept_timeout_s: int,
        pickup_delay_s: int,
        dropoff_delay_s: int,
        client: Client | None = None,  # tests inject; production connects lazily
    ):
        self._address = address
        self._task_queue = task_queue
        self._accept_timeout_s = accept_timeout_s
        self._pickup_delay_s = pickup_delay_s
        self._dropoff_delay_s = dropoff_delay_s
        self._client = client
        self._lock = asyncio.Lock()

    async def _connect(self) -> Client:
        if self._client is None:  # pragma: no cover — live connection path;
            # tests always inject. The lock serializes concurrent first-starts.
            async with self._lock:
                if self._client is None:
                    self._client = await Client.connect(self._address)
        return self._client

    async def start(self, order_id: str) -> None:
        client = await self._connect()
        try:
            await client.start_workflow(
                "OrderWorkflow",
                WorkflowInput(
                    order_id=order_id,
                    accept_timeout_s=self._accept_timeout_s,
                    pickup_delay_s=self._pickup_delay_s,
                    dropoff_delay_s=self._dropoff_delay_s,
                ),
                id=f"ord::{order_id}",
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            log.info("saga started", order_id=order_id)
        except WorkflowAlreadyStartedError:
            # The idempotent-replay path: the workflow exists; that IS the goal.
            log.info("saga already running", order_id=order_id)
