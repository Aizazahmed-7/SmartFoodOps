"""The real SagaPort — placement's front door (ADR-0023).

Starts OrderWorkflow BY STRING NAME so the API process never imports
workflow code (uvicorn stays sandbox-free; only the worker loads
workflows.py). Workflow id = ord::{order_id}, and since the order id is
itself derived from the idempotency key, the same intent always addresses
the same workflow — Temporal referees duplicate placements for us.

Two policies, two different questions, easy to confuse:
  * id_conflict_policy = USE_EXISTING — "the workflow is RUNNING: attach
    to it instead of erroring." This is what makes a client retry safe.
  * id_reuse_policy = REJECT_DUPLICATE — "the workflow is CLOSED: refuse
    to reuse the id." This is what stops a settled order from being
    resurrected by a replayed request.

The Temporal connection is lazy: created on first use, cached. create_app
stays synchronous and tests that never place orders never connect.
"""

import asyncio
from datetime import timedelta

from smartfood_otel import get_logger
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowUpdateFailedError,
    WorkflowUpdateRPCTimeoutOrCancelledError,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from ..domain.ports import PlacementPending, SagaClosed, SagaGone, SagaUnavailable
from ..values import (
    SIGNAL_CANCEL_REQUESTED,
    SIGNAL_FOOD_READY,
    SIGNAL_RESTAURANT_DECISION,
    UPDATE_AWAIT_PLACEMENT,
    PlacementAck,
    PlacementInput,
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
        forward_deadline_s: int = 300,
        await_seconds: float = 2.0,
        client: Client | None = None,  # tests inject; production connects lazily
    ):
        self._address = address
        self._task_queue = task_queue
        self._accept_timeout_s = accept_timeout_s
        self._pickup_delay_s = pickup_delay_s
        self._dropoff_delay_s = dropoff_delay_s
        self._forward_deadline_s = forward_deadline_s
        self._await_seconds = await_seconds
        self._client = client
        self._lock = asyncio.Lock()

    async def _connect(self) -> Client:
        if self._client is None:  # pragma: no cover — live connection path;
            # tests always inject. The lock serializes concurrent first-starts.
            async with self._lock:
                if self._client is None:
                    self._client = await Client.connect(
                        self._address,
                        # OTel spans for every start/update/signal, and the
                        # trace context rides INTO the workflow via payload
                        # headers — the worker's interceptor picks it up.
                        interceptors=[TracingInterceptor()],
                    )
        return self._client

    async def place(self, placement: PlacementInput) -> PlacementAck | PlacementPending:
        """Start the saga and wait for it to make the order (ADR-0023).

        ONE RPC does both. execute_update_with_start_workflow sends the
        start and the await_placement update together (Temporal calls this
        ExecuteMultiOperation) and returns when the UPDATE resolves — which
        the workflow does the moment create_order commits. Sending them
        separately would open a gap where the start succeeded and the update
        never got sent, which is the exact class of hole this design set out
        to close.

        rpc_timeout is the customer's patience, not the workflow's: when it
        expires the placement is still durably running, so we answer
        PlacementPending rather than an error."""
        client = await self._connect()
        start = WithStartWorkflowOperation(
            "OrderWorkflow",
            WorkflowInput(
                placement=placement,
                accept_timeout_s=self._accept_timeout_s,
                pickup_delay_s=self._pickup_delay_s,
                dropoff_delay_s=self._dropoff_delay_s,
                forward_deadline_s=self._forward_deadline_s,
            ),
            id=f"ord::{placement.order_id}",
            task_queue=self._task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        try:
            ack = await client.execute_update_with_start_workflow(
                UPDATE_AWAIT_PLACEMENT,
                start_workflow_operation=start,
                result_type=PlacementAck,  # by-name call: name the shape
                rpc_timeout=timedelta(seconds=self._await_seconds),
            )
        except WorkflowUpdateRPCTimeoutOrCancelledError:
            log.warning(
                "placement await timed out — workflow continues", order_id=placement.order_id
            )
            return PlacementPending(placement.order_id)
        except WorkflowUpdateFailedError as exc:
            # The update itself failed — reachable only if create_order ever
            # gains a non-retryable failure (today it has none, so a broken
            # database means infinite retries and a PlacementPending answer
            # instead). Mapping it keeps that future change from surfacing as
            # a raw 500: the customer is told "try again", which is true.
            log.warning("placement update failed", order_id=placement.order_id, error=str(exc))
            raise SagaUnavailable(str(exc)) from None
        except WorkflowAlreadyStartedError:
            # REJECT_DUPLICATE refused a CLOSED execution: this key was
            # reused after its replay TTL. The domain owns the answer (the
            # old order, if it is still there) because it owns the database.
            raise SagaClosed(placement.order_id) from None
        except RPCError as exc:
            if exc.status == RPCStatusCode.DEADLINE_EXCEEDED:
                return PlacementPending(placement.order_id)
            if exc.status == RPCStatusCode.ALREADY_EXISTS:
                raise SagaClosed(placement.order_id) from None
            raise SagaUnavailable(str(exc)) from None
        except OSError as exc:  # DNS/socket failures below the RPC layer
            raise SagaUnavailable(str(exc)) from None
        log.info("saga placed", order_id=placement.order_id, status=ack.status)
        return ack

    async def attach_placement(self, order_id: str) -> PlacementAck | PlacementPending:
        """Update-only (no start): ask a running ord::{order_id} for its
        placement ack. The await_placement handler is a pure read of state
        already set, so re-running it for a late caller is free — and if
        create_order has not committed yet, we park exactly like place()
        does, under the same await budget.

        NOT_FOUND covers both "never started" and "already finished" (the
        same collapse _signal documents); both map to SagaGone — for a
        finished workflow the orders row exists and the caller answered
        from it before ever pricing, so reaching here means the refusal
        should stand."""
        try:
            client = await self._connect()
            ack = await client.get_workflow_handle(f"ord::{order_id}").execute_update(
                UPDATE_AWAIT_PLACEMENT,
                result_type=PlacementAck,
                rpc_timeout=timedelta(seconds=self._await_seconds),
            )
        except WorkflowUpdateRPCTimeoutOrCancelledError:
            return PlacementPending(order_id)
        except WorkflowUpdateFailedError as exc:
            raise SagaUnavailable(str(exc)) from None
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise SagaGone(f"ord::{order_id}") from None
            if exc.status == RPCStatusCode.DEADLINE_EXCEEDED:
                return PlacementPending(order_id)
            raise SagaUnavailable(str(exc)) from None
        except OSError as exc:
            raise SagaUnavailable(str(exc)) from None
        log.info("placement attached", order_id=order_id, status=ack.status)
        return ack

    async def signal_decision(self, order_id: str, verdict: Verdict) -> None:
        await self._signal(f"ord::{order_id}", SIGNAL_RESTAURANT_DECISION, verdict)
        log.info("decision signalled", order_id=order_id, verdict=verdict)

    async def signal_food_ready(self, order_id: str) -> None:
        await self._signal(f"dlv::{order_id}", SIGNAL_FOOD_READY)
        log.info("food_ready signalled", order_id=order_id)

    async def signal_cancel(self, order_id: str) -> None:
        await self._signal(f"ord::{order_id}", SIGNAL_CANCEL_REQUESTED)
        log.info("cancel signalled", order_id=order_id)

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
