"""OrderWorkflow — the single writer of the order's saga (ADR-0001).

Runs INSIDE Temporal's deterministic sandbox: this module may import only
stdlib, temporalio, and order.values (passed through). No SQLAlchemy, no
httpx, no datetime.now()/uuid4() — time comes from timers, identity from
the input, and every side effect happens in an activity invoked BY NAME.

S5 scope: PLACED → VALIDATED → PAYMENT_CLEARED → CONFIRMED → the durable
restaurant_decision wait (signal vs timer), with the §7 compensation
unwind for every failure. accept ends at ACCEPTED; S6 extends from there
(DeliveryWorkflow child, capture, settle).

Action budget (happy path): 5 activities + 1 timer + 1 signal — well
inside ≤12/≤3/≤4 (ADR-0018).
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from order.values import (
        ActivityName,
        CancelReason,
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
            await self._step(ActivityName.MARK_ACCEPTED, order_id)
            return "ACCEPTED"  # S6 continues from here (child + capture)

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
        """The §7 unwind, in reverse order of acquisition: mark CANCELLING,
        void the payment hold, release the reservation, mark CANCELLED.
        Every step retries forever (5-min cap) — compensations are never
        silently dropped."""
        await self._step(ActivityName.BEGIN_CANCEL, order_id, expected)
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
