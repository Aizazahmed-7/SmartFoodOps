"""OrderWorkflow — the single writer of the order's saga (ADR-0001).

Runs INSIDE Temporal's deterministic sandbox: this module may import only
stdlib, temporalio, and order.values (passed through). No SQLAlchemy, no
httpx, no datetime.now()/uuid4() — time comes from timers, identity from
the input, and every side effect happens in an activity invoked BY NAME.

ADR-0023: the workflow also OWNS placement. It is started by the POST
itself (update-with-start), creates the order row in its first activity,
and answers the waiting request through the await_placement update — so
the order exists because the saga made it, not before the saga knew.

S5: PLACED → VALIDATED → PAYMENT_CLEARED → CONFIRMED → the durable
restaurant_decision wait (signal vs timer), with the §7 compensation
unwind for every failure. S6: accept starts the DeliveryWorkflow child
(simulated courier), waits for it to deliver, then captures the payment
and settles — the full FR-19 lifecycle. S7: a cancel_requested signal is
honored at both wait points — pre-accept the customer always wins; during
the kitchen window the DB referees customer-vs-courier (set-guarded
TRY_BEGIN_CANCEL); from PICKED_UP on, cancels lose (FR-21). Capture
happens only after delivery (FR-20), so a customer cancel can never need
a refund — every honored cancel voids an uncaptured hold.

The child starts BEFORE mark_accepted on purpose: ACCEPTED becoming
visible in the DB is the kitchen's licence to hit /preparing → /ready,
and /ready signals dlv::{order_id} — starting the child first means that
signal always has a target (no ordering race to apologize for).

Action budget (happy path): 7 activities + 1 child + 1 timer + 1 signal +
1 update — inside ≤12/≤3/≤4 (ADR-0018); the child adds 2 activities,
2 timers, 1 signal of its own. (create_order replaced the price_order
local activity one-for-one — placement moved in without growing the
budget, because the workflow no longer reads back what it was told.)
"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from order.values import (
        SIGNAL_COURIER_DELIVERED,
        SIGNAL_COURIER_PICKED_UP,
        SIGNAL_FOOD_READY,
        SIGNAL_OFFER_ACCEPTED,
        UPDATE_AWAIT_PLACEMENT,
        ActivityName,
        CancelReason,
        DeliveryInput,
        PlacementAck,
        Verdict,
        WorkflowInput,
        price_of,
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


class _RanOutOfTime(Exception):
    """A forward step exhausted its deadline. Raised and caught INSIDE this
    module — it never escapes run(), so it can never become a workflow
    failure. It exists so the unwind can tell "the world is unreachable"
    (recoverable: cancel the order cleanly) from "the world disagrees with
    our history" (an IllegalTransition — a page, not a cancel)."""


@workflow.defn(name="OrderWorkflow")
class OrderWorkflow:
    def __init__(self) -> None:
        self._decision: Verdict | None = None
        self._cancel_requested = False
        self._placed: PlacementAck | None = None
        self._deadline = timedelta(seconds=300)  # replaced from input in run()

    @workflow.update(name=UPDATE_AWAIT_PLACEMENT)
    async def await_placement(self) -> PlacementAck:
        """The HTTP request's handle on this workflow (ADR-0023).

        An update is a signal that answers. The placement POST sends this
        one WITH the start (a single ExecuteMultiOperation RPC), then blocks
        on it: the handler waits for the create_order activity to commit and
        hands back the id and status the customer gets in their 202.

        Blocking in an update handler is legal and cheap — it parks on the
        workflow's own event loop, holding no worker thread. If the caller
        gives up first, the handler simply completes into a void: the client
        is gone, the workflow is untouched, and the order still gets made."""
        await workflow.wait_condition(lambda: self._placed is not None)
        assert self._placed is not None  # wait_condition's postcondition
        return self._placed

    @workflow.signal(name="restaurant_decision")
    def restaurant_decision(self, verdict: Verdict) -> None:
        if self._decision is None:  # first verdict wins; duplicates no-op
            self._decision = verdict

    @workflow.signal(name="cancel_requested")
    def cancel_requested(self) -> None:
        self._cancel_requested = True  # a flag: duplicates collapse (S7)

    @workflow.run
    async def run(self, input: WorkflowInput) -> str:
        placement = input.placement
        order_id = placement.order_id

        # Step one is now the order itself: the row, its lines, the outbox
        # fact and the idempotent answer, in one transaction. Only once it
        # has COMMITTED does the waiting POST get its 202 — the customer is
        # never told "placed" about a row that does not exist.
        #
        # Deliberately NOT deadline-bounded (_step, not _forward): it holds
        # nothing, so there is nothing to unwind, and a workflow that failed
        # here would poison this idempotency key permanently — every retry
        # re-derives the same ord:: id, which REJECT_DUPLICATE then refuses
        # forever. Waiting for the database to come back is the right answer.
        status = str(await self._step(ActivityName.CREATE_ORDER, placement))
        self._placed = PlacementAck(order_id=order_id, status=status)

        # No price_order activity any more: the API priced this cart before
        # starting us and the numbers travel in our input, so re-reading
        # them from the DB would be a round trip to learn what we were told.
        price = price_of(placement)

        # The three pre-confirmation steps run under a DEADLINE (see
        # _forward). `stage` tracks the last state the DB is known to hold,
        # so a deadline unwind can name it in the guarded transition.
        self._deadline = timedelta(seconds=input.forward_deadline_s)
        stage = "PLACED"
        try:
            reserved = await self._forward(ActivityName.VALIDATE_AND_RESERVE, order_id, price)
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

            stage = "VALIDATED"
            authorized = await self._forward(ActivityName.AUTHORIZE_PAYMENT, order_id, price)
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

            stage = "PAYMENT_CLEARED"
            await self._forward(ActivityName.CONFIRM_ORDER, order_id)
        except _RanOutOfTime:
            # A dependency stayed unreachable past the deadline. Unwind with
            # the AMBIGUOUS case in mind: the step that ran out of time may
            # have half-succeeded (its last attempt could have committed and
            # lost the answer), so undo everything it could have acquired.
            # Both undos are idempotent — releasing a reservation that was
            # never made, or voiding an authorization that never happened,
            # are no-ops by contract, not errors.
            await self._cancel(
                order_id,
                expected=stage,
                reason=CancelReason.SYSTEM_TIMEOUT,
                void=stage != "PLACED",  # nothing was authorized before VALIDATED
                release=True,
            )
            return "CANCELLED"

        # FR-18: the durable wait — restaurant decision vs the timeout timer,
        # now also listening for the customer's cancel (S7).
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None or self._cancel_requested,
                timeout=timedelta(seconds=input.accept_timeout_s),
            )
        except TimeoutError:
            self._decision = None  # explicit: the timer decided

        if self._cancel_requested:
            # The customer beat the restaurant to the fork. Pre-accept the
            # customer always wins — nothing is cooking yet.
            await self._cancel(
                order_id,
                expected="CONFIRMED",
                reason=CancelReason.CUSTOMER_CANCELLED,
                void=True,
                release=True,
            )
            return "CANCELLED"

        if self._decision == "accept":
            # Child first (see module docstring), then the ACCEPTED mark that
            # licences the kitchen to drive it. REQUEST_CANCEL: if this parent
            # is ever cancelled, the courier is told to stand down too.
            child = await workflow.start_child_workflow(
                DeliveryWorkflow.run,
                DeliveryInput(
                    order_id=order_id,
                    offer_first_timeout_s=input.offer_first_timeout_s,
                    offer_next_timeout_s=input.offer_next_timeout_s,
                    no_rider_deadline_s=input.no_rider_deadline_s,
                    no_candidates_retry_s=input.no_candidates_retry_s,
                    pickup_timeout_s=input.pickup_timeout_s,
                ),
                id=f"dlv::{order_id}",
                parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
            )
            await self._step(ActivityName.MARK_ACCEPTED, order_id)

            if not await self._deliver_or_cancel(child, order_id):
                return "CANCELLED"

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

    async def _deliver_or_cancel(self, child, order_id: str) -> bool:
        """Wait for the courier while listening for the customer's cancel.
        True = delivered (proceed to capture+settle); False = cancelled.

        The race is refereed by the DATABASE, not by signal timing: the
        set-guarded TRY_BEGIN_CANCEL flips {ACCEPTED, PREPARING, READY} to
        CANCELLING or answers too_late if the courier's PICKED_UP landed
        first — whichever write wins the row wins the argument (FR-21)."""
        cancel_wakeup = asyncio.ensure_future(
            workflow.wait_condition(lambda: self._cancel_requested)
        )
        try:
            # workflow.wait, not asyncio.wait: the stdlib version iterates a
            # SET (order varies across processes), so the sandbox flags it —
            # the SDK's drop-in preserves order and replays deterministically.
            await workflow.wait([child, cancel_wakeup], return_when=asyncio.FIRST_COMPLETED)
            if not child.done():
                outcome = await self._step(
                    ActivityName.TRY_BEGIN_CANCEL, order_id, CancelReason.CUSTOMER_CANCELLED
                )
                if outcome == "ok":
                    child.cancel()
                    # Drain the child: it ends cancelled, or — if it raced a
                    # mark against the now-CANCELLING row — failed on the
                    # guard. Either way the row is safely CANCELLING.
                    with suppress(Exception, asyncio.CancelledError):
                        await child
                    await self._unwind(
                        order_id, reason=CancelReason.CUSTOMER_CANCELLED, void=True, release=True
                    )
                    return False
                # too_late: the courier already holds the food — the cancel
                # loses and the order rides to delivery like any other.
            outcome = await child  # or surface the child's real failure
            if outcome == "NO_RIDER":
                # FR-32: READY, cooked, and nobody came inside the deadline.
                # Set-guarded like the customer's kitchen-window cancel — the
                # DB referees the (near-impossible) race where a pickup
                # landed as the deadline fired; too_late = ride to delivery.
                verdict = await self._step(
                    ActivityName.TRY_BEGIN_CANCEL, order_id, CancelReason.NO_RIDER_AVAILABLE
                )
                if verdict == "ok":
                    await self._step(ActivityName.CANCEL_DISPATCH, order_id)
                    await self._unwind(
                        order_id,
                        reason=CancelReason.NO_RIDER_AVAILABLE,
                        void=True,
                        release=True,
                    )
                    return False
            return True
        finally:
            cancel_wakeup.cancel()

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
        (reason stamped up front — the unwind window is unbounded), then the
        shared undo tail."""
        await self._step(ActivityName.BEGIN_CANCEL, order_id, expected, reason)
        await self._unwind(order_id, reason=reason, void=void, release=release)

    async def _unwind(
        self, order_id: str, *, reason: CancelReason, void: bool, release: bool
    ) -> None:
        """Void the payment hold, release the reservation, mark CANCELLED.
        Every step retries forever (5-min cap) — compensations are never
        silently dropped."""
        if void:
            await self._step(ActivityName.VOID_AUTHORIZATION, order_id)
        if release:
            await self._step(ActivityName.RELEASE_RESERVATION, order_id)
        await self._step(ActivityName.FINISH_CANCEL, order_id, reason)

    async def _forward(self, name: str, *args: object) -> object:
        """A forward step, bounded by a deadline.

        Compensations retry forever because dropping an unwind strands money
        or stock. Forward steps must NOT: they hold a reservation the
        inventory reaper releases at 1800s, so retrying an authorization for
        an hour would eventually be charging a card for stock somebody else
        now owns. When the deadline expires we cancel the order cleanly
        instead — a customer told "we could not complete this" is far better
        served than one whose order silently waits forever.

        Reading the failure (verified against the SDK, not assumed): with no
        maximum_attempts in RETRY, execute_activity can only fail two ways —
        a NON-RETRYABLE ApplicationError (IllegalTransition: the world
        disagrees with our history, which retrying can never fix), or the
        deadline running out. In the second case the cause is whatever the
        last attempt raised (an ordinary ApplicationError), or a TimeoutError
        when no attempt ever got to run. So the discriminator is the
        non_retryable flag, not the exception type."""
        try:
            return await workflow.execute_activity(
                name,
                args=list(args),
                start_to_close_timeout=STEP_TIMEOUT,
                schedule_to_close_timeout=self._deadline,
                retry_policy=RETRY,
            )
        except ActivityError as exc:
            if isinstance(exc.cause, ApplicationError) and exc.cause.non_retryable:
                raise  # a genuine fault — fail loudly, exactly as before
            raise _RanOutOfTime(name) from None

    async def _step(self, name: str, *args: object) -> object:
        return await workflow.execute_activity(
            name,
            args=list(args),
            start_to_close_timeout=STEP_TIMEOUT,
            retry_policy=RETRY,
        )


@workflow.defn(name="DeliveryWorkflow")
class DeliveryWorkflow:
    """The delivery, driven by a REAL courier (child of OrderWorkflow,
    id dlv::{order_id}) — the dispatch milestone's replacement for the
    S6 timer-courier, exactly as that version's docstring promised: the
    id and the kitchen's food_ready signal are unchanged.

    Shape: wait for the kitchen → the OFFER CASCADE (find_and_offer
    activity reserves a rider via dispatch's DDB lock; a 15s/12s window
    waits for the accept SIGNAL; a miss revokes and moves on, FR-29) →
    RECORD_RIDER → the FR-30 pickup deadline (no pickup in time =
    conditional revoke + back to the cascade) → the courier's pickup and
    delivery signals drive the marks. The READY-unassigned deadline
    (FR-32) ends the cascade with "NO_RIDER" and the PARENT cancels
    through the normal compensation path.

    Signals arrive from dispatch via order's internal courier endpoint
    (dispatch never touches Temporal). Every signal is a flag or an
    idempotent map write — duplicates and late arrivals collapse. The
    post-pickup wait is UNBOUNDED on purpose (FR-32: once the food is
    with the rider, never auto-cancel; the delivery_at_risk ops queue is
    the named deferral).

    On cancellation (customer cancel, parent unwind) the cleanup frees
    whatever dispatch holds — a cancelled order must never strand a
    locked or assigned rider.

    Action budget: each cascade attempt costs ≤2 activities + 1 timer;
    the deadline (600s) over the offer windows (12–15s) bounds attempts
    far below Temporal's per-workflow limits, and the no-candidates
    breather (10s) bounds the empty-city spin the same way."""

    def __init__(self) -> None:
        self._food_ready = False
        self._accepted: dict[str, str] = {}  # offer_id → rider_id
        # Rider-SCOPED on purpose: after a pickup-timeout revoke, a ghost
        # rider's late signals must never advance the NEW courier's
        # delivery — each wait below checks the current rider's own set.
        self._picked_up: set[str] = set()
        self._delivered: set[str] = set()

    @workflow.signal(name=SIGNAL_FOOD_READY)
    def food_ready(self) -> None:
        self._food_ready = True  # duplicates collapse into the same truth

    @workflow.signal(name=SIGNAL_OFFER_ACCEPTED)
    def offer_accepted(self, offer_id: str, rider_id: str) -> None:
        self._accepted[offer_id] = rider_id

    @workflow.signal(name=SIGNAL_COURIER_PICKED_UP)
    def courier_picked_up(self, rider_id: str) -> None:
        self._picked_up.add(rider_id)

    @workflow.signal(name=SIGNAL_COURIER_DELIVERED)
    def courier_delivered(self, rider_id: str) -> None:
        self._delivered.add(rider_id)

    @workflow.run
    async def run(self, input: DeliveryInput) -> str:
        order_id = input.order_id
        try:
            await workflow.wait_condition(lambda: self._food_ready)
            rider = await self._assign(input)
            if rider is None:
                return "NO_RIDER"  # the parent cancels through §7
            await self._step(ActivityName.MARK_PICKED_UP, order_id)
            await workflow.wait_condition(lambda: rider in self._delivered)
            await self._step(ActivityName.MARK_DELIVERED, order_id)
            return "DELIVERED"
        except asyncio.CancelledError:
            # Freed BEFORE propagating: the order is dying, and dispatch
            # may hold a lock or an assignment a rider is staring at.
            with suppress(Exception):
                await self._step(ActivityName.CANCEL_DISPATCH, order_id)
            raise

    async def _assign(self, input: DeliveryInput) -> str | None:
        """The cascade + the pickup-liveness loop. A rider who accepts but
        never picks up is revoked and the cascade resumes without them —
        the SAME deadline governs throughout, so a parade of ghosts still
        ends in NO_RIDER rather than forever."""
        deadline = workflow.now() + timedelta(seconds=input.no_rider_deadline_s)
        exclude: list[str] = []
        attempt = 0
        while True:
            rider: str | None = None
            while rider is None:
                if workflow.now() >= deadline:
                    return None
                attempt += 1
                offer = await self._step_dict(
                    ActivityName.FIND_AND_OFFER, input.order_id, attempt, exclude
                )
                if offer["outcome"] != "offered":
                    await workflow.sleep(timedelta(seconds=input.no_candidates_retry_s))
                    # A city emptied BY OUR OWN exclusions gets a fresh
                    # round: a rider who missed one window is not a ghost
                    # forever (found live: a one-courier town deadlocked
                    # after a single missed offer). The deadline still
                    # bounds the whole affair.
                    exclude.clear()
                    continue
                offer_id, candidate = str(offer["offer_id"]), str(offer["rider_id"])
                window = input.offer_first_timeout_s if attempt == 1 else input.offer_next_timeout_s
                try:
                    await workflow.wait_condition(
                        lambda oid=offer_id: oid in self._accepted,
                        timeout=timedelta(seconds=window),
                    )
                    rider = self._accepted[offer_id]
                except TimeoutError:
                    expired = await self._step_dict(
                        ActivityName.EXPIRE_OFFER, input.order_id, offer_id, candidate
                    )
                    if expired["outcome"] == "already_assigned":
                        # The accept beat the revoke inside DDB but its
                        # signal lost the race (or the wire) — the revoke's
                        # read IS the recovery. Nothing is retried, nothing
                        # is lost.
                        rider = str(expired["rider_id"])
                    else:
                        exclude.append(candidate)
            await self._step(ActivityName.RECORD_RIDER, input.order_id, rider)
            try:
                await workflow.wait_condition(
                    lambda current=rider: current in self._picked_up,
                    timeout=timedelta(seconds=input.pickup_timeout_s),
                )
                return rider
            except TimeoutError:
                revoked = await self._step_dict(
                    ActivityName.UNASSIGN_STALLED, input.order_id, rider
                )
                if revoked["outcome"] == "already_picked_up":
                    return rider  # the scan beat the deadline — ride on
                exclude.append(rider)  # strike: back to the cascade

    async def _step(self, name: str, *args: object) -> object:
        return await workflow.execute_activity(
            name,
            args=list(args),
            start_to_close_timeout=STEP_TIMEOUT,
            retry_policy=RETRY,
        )

    async def _step_dict(self, name: str, *args: object) -> dict[str, Any]:
        """The dispatch activities answer OUTCOME DICTS (their port's
        contract) — one typed doorway instead of six casts."""
        return cast("dict[str, Any]", await self._step(name, *args))
