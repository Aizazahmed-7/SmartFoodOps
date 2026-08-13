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
from temporalio.service import RPCError, RPCStatusCode

from ..domain.ports import SagaGone, SagaUnavailable
from ..values import (
    SIGNAL_FOOD_READY,
    SIGNAL_RESTAURANT_DECISION,
    Verdict,
    WorkflowInput,
)

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

    async def signal_decision(self, order_id: str, verdict: Verdict) -> None:
        await self._signal(f"ord::{order_id}", SIGNAL_RESTAURANT_DECISION, verdict)
        log.info("decision signalled", order_id=order_id, verdict=verdict)

    async def signal_food_ready(self, order_id: str) -> None:
        await self._signal(f"dlv::{order_id}", SIGNAL_FOOD_READY)
        log.info("food_ready signalled", order_id=order_id)

    async def _signal(self, workflow_id: str, name: str, *args: object) -> None:
        """RPC → domain translation: NOT_FOUND means the workflow already
        finished (the window closed — a business answer the route maps);
        anything else is transport trouble the caller may retry. The lazy
        _connect sits INSIDE the try: a first-signal-while-Temporal-is-down
        must surface as 503, not a raw 500."""
        try:
            client = await self._connect()
            await client.get_workflow_handle(workflow_id).signal(name, *args)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise SagaGone(workflow_id) from None
            raise SagaUnavailable(str(exc)) from None
        except OSError as exc:  # DNS/socket failures below the RPC layer
            raise SagaUnavailable(str(exc)) from None
