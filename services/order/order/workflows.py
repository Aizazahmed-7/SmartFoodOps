"""OrderWorkflow — the single writer of the order's saga (ADR-0001).

Runs INSIDE Temporal's deterministic sandbox: this module may import only
stdlib, temporalio, and order.values (passed through). No SQLAlchemy, no
httpx, no datetime.now()/uuid4() — time comes from timers, identity from
the input, and every side effect happens in an activity invoked BY NAME.

S5: PLACED → VALIDATED → PAYMENT_CLEARED → CONFIRMED → the durable
restaurant_decision wait (signal vs timer), with the §7 compensation
unwind for every failure. S6: accept starts the DeliveryWorkflow child
(simulated courier), waits for it to deliver, then captures the payment
and settles — the full FR-19 lifecycle.

The child starts BEFORE mark_accepted on purpose: ACCEPTED becoming
visible in the DB is the kitchen's licence to hit /preparing → /ready,
and /ready signals dlv::{order_id} — starting the child first means that
signal always has a target (no ordering race to apologize for).

Action budget (happy path): 7 activities + 1 local + 1 child + 1 timer +
1 signal — inside ≤12/≤3/≤4 (ADR-0018); the child adds 2 activities,
2 timers, 1 signal of its own.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from order.values import (
        ActivityName,
        CancelReason,
        DeliveryInput,
        PriceResult,
        Verdict,
        WorkflowInput,
    )

# Transient failures retry forever with a 5-minute cap (compensations must
# never be dropped — §7); IllegalTransition is marked non-retryable by the
# activity itself and stops the retry loop immediately.
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
)
STEP_TIMEOUT = timedelta(seconds=30)  # per-attempt; retries continue beyond


@workflow.defn(name="OrderWorkflow")
class OrderWorkflow:
    def __init__(self) -> None:
        self._decision: Verdict | None = None

    @workflow.signal(name="restaurant_decision")
    def restaurant_decision(self, verdict: Verdict) -> None:
        if self._decision is None:  # first verdict wins; duplicates no-op
            self._decision = verdict

    @workflow.run
    async def run(self, input: WorkflowInput) -> str:
        order_id = input.order_id

        price = await workflow.execute_local_activity(
            ActivityName.PRICE_ORDER,
            order_id,
            result_type=PriceResult,  # by-name call: tell the converter the shape
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RETRY,
        )

        reserved = await self._step(ActivityName.VALIDATE_AND_RESERVE, order_id, price)
        if reserved != "ok":
            # Nothing is held yet — no compensation, straight to cancelled
            # (§7 row 1). The failed reserve was all-or-nothing in inventory.
            reason = (
                CancelReason.ITEM_UNAVAILABLE
                if reserved == "item_unavailable"
                else CancelReason.AT_CAPACITY
            )
            await self._cancel(order_id, expected="PLACED", reason=reason)
            return "CANCELLED"

        authorized = await self._step(ActivityName.AUTHORIZE_PAYMENT, order_id, price)
        if authorized != "ok":
            # Reserved but not charged: release the stock, no void (§7 row 2
            # — there is no authorization to void).
            await self._cancel(
                order_id,
                expected="VALIDATED",
                reason=CancelReason.PAYMENT_DECLINED,
                release=True,
            )
            return "CANCELLED"

        await self._step(ActivityName.CONFIRM_ORDER, order_id)

        # FR-18: the durable wait — restaurant decision vs the timeout timer.
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=input.accept_timeout_s),
            )
        except TimeoutError:
            self._decision = None  # explicit: the timer decided

        if self._decision == "accept":
            # Child first (see module docstring), then the ACCEPTED mark that
            # licences the kitchen to drive it. REQUEST_CANCEL: if this parent
            # is ever cancelled (S7), the courier is told to stand down too.
            child = await workflow.start_child_workflow(
                DeliveryWorkflow.run,
                DeliveryInput(
                    order_id=order_id,
                    pickup_delay_s=input.pickup_delay_s,
                    dropoff_delay_s=input.dropoff_delay_s,
                ),
                id=f"dlv::{order_id}",
                parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
            )
            await self._step(ActivityName.MARK_ACCEPTED, order_id)
            await child  # kitchen preps, courier drives, DELIVERED lands

            # Money and stock become final only after the food actually
            # arrived (FR-20): take the held funds, consume the reservation.
            await self._step(ActivityName.CAPTURE_PAYMENT, order_id)
            await self._step(ActivityName.SETTLE_ORDER, order_id)
            return "SETTLED"

        # reject or timeout: full unwind — void the hold, release the stock
        # (§7 row 4: reverse order of acquisition).
        reason = (
            CancelReason.RESTAURANT_REJECTED
            if self._decision == "reject"
            else CancelReason.RESTAURANT_TIMEOUT
        )
        await self._cancel(order_id, expected="CONFIRMED", reason=reason, void=True, release=True)
        return "CANCELLED"

    # ── helpers ────────────────────────────────────────────────────

    async def _cancel(
        self,
        order_id: str,
        *,
        expected: str,
        reason: CancelReason,
        void: bool = False,
        release: bool = False,
    ) -> None:
        """The §7 unwind, in reverse order of acquisition: mark CANCELLING
        (reason stamped up front — the unwind window is unbounded), void the
        payment hold, release the reservation, mark CANCELLED. Every step
        retries forever (5-min cap) — compensations are never silently
        dropped."""
        await self._step(ActivityName.BEGIN_CANCEL, order_id, expected, reason)
        if void:
            await self._step(ActivityName.VOID_AUTHORIZATION, order_id)
        if release:
            await self._step(ActivityName.RELEASE_RESERVATION, order_id)
        await self._step(ActivityName.FINISH_CANCEL, order_id, reason)

    async def _step(self, name: str, *args: object) -> object:
        return await workflow.execute_activity(
            name,
            args=list(args),
            start_to_close_timeout=STEP_TIMEOUT,
            retry_policy=RETRY,
        )


@workflow.defn(name="DeliveryWorkflow")
class DeliveryWorkflow:
    """The simulated courier (child of OrderWorkflow, id dlv::{order_id}).

    Waits for the kitchen's food_ready signal — an UNBOUNDED durable wait:
    kitchens take as long as they take, and a parked workflow costs nothing
    (a kitchen-SLA timeout is W3 hardening, alongside the reservation-TTL
    interplay it would close). Then two timers stand in for the drive:
    pickup → PICKED_UP, dropoff → DELIVERED. Real dispatch replaces the
    timers next milestone; the id and signal names are the stable contract.
    """

    def __init__(self) -> None:
        self._food_ready = False

    @workflow.signal(name="food_ready")
    def food_ready(self) -> None:
        self._food_ready = True  # duplicates collapse into the same truth

    @workflow.run
    async def run(self, input: DeliveryInput) -> str:
        await workflow.wait_condition(lambda: self._food_ready)
        await workflow.sleep(timedelta(seconds=input.pickup_delay_s))
        await self._step(ActivityName.MARK_PICKED_UP, input.order_id)
        await workflow.sleep(timedelta(seconds=input.dropoff_delay_s))
        await self._step(ActivityName.MARK_DELIVERED, input.order_id)
        return "DELIVERED"

    async def _step(self, name: str, *args: object) -> object:
        return await workflow.execute_activity(
            name,
            args=list(args),
            start_to_close_timeout=STEP_TIMEOUT,
            retry_policy=RETRY,
        )
