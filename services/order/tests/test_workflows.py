"""OrderWorkflow + DeliveryWorkflow against Temporal's time-skipping test
server: the decision matrix, the delivered→captured→settled tail, every
compensation branch, retry behavior, duplicate signals, and the
REJECT_DUPLICATE contract.

Workflow-logic tests use MOCK activities (the real ones have their own
sqlite suite) registered under the same names, and the unsandboxed runner
so coverage traces workflow code. ONE test runs the default sandbox to
prove import purity for BOTH workflows.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from order.values import (
    UPDATE_AWAIT_PLACEMENT,
    ActivityName,
    LineSnapshot,
    LineSpec,
    PlacementAck,
    PlacementInput,
    PriceResult,
    WorkflowInput,
)
from order.workflows import DeliveryWorkflow, OrderWorkflow
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

pytestmark = pytest.mark.asyncio(loop_scope="module")

PRICE = PriceResult(
    restaurant_id="rst_1",
    amount_cents=3446,
    currency="USD",
    card_token="tok_ok",
    lines=[LineSpec(item_id="itm_a", qty=2)],
)


def placement_for(order_id: str) -> PlacementInput:
    """The workflow's input carries the whole priced order now — and the
    numbers here MUST project to PRICE, since price_of() is what the money
    activities see (ADR-0023 deleted the price_order round trip)."""
    return PlacementInput(
        order_id=order_id,
        request_hash=f"hash-of-{order_id}",
        user_id="usr_1",
        restaurant_id="rst_1",
        restaurant_name="Biryani House",
        card_token="tok_ok",
        menu_version=3,
        currency="USD",
        amount_cents=3446,
        placed_at="2026-08-18T10:00:00+00:00",
        lines=[
            LineSnapshot(
                menu_item_id="itm_a",
                name="Chicken Biryani",
                unit_price_cents=1500,
                qty=2,
                line_total_cents=3000,
            )
        ],
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    # Downloads the test-server binary on first ever run; cached after.
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


def mock_activities(script: dict | None = None):
    """The workflow's world, scripted: `script` overrides per-activity
    returns (a list means consecutive calls pop; an Exception raises)."""
    script = dict(script or {})
    calls: list[tuple] = []

    def outcome(name: str, default: object) -> object:
        value = script.get(name, default)
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    @activity.defn(name=ActivityName.CREATE_ORDER)
    async def create_order(placement: PlacementInput) -> str:
        calls.append(("create_order", placement.order_id))
        return outcome("create_order", "PLACED")  # type: ignore[return-value]

    @activity.defn(name=ActivityName.VALIDATE_AND_RESERVE)
    async def validate_and_reserve(order_id: str, price: PriceResult) -> str:
        calls.append(("validate_and_reserve", order_id))
        return outcome("validate_and_reserve", "ok")  # type: ignore[return-value]

    @activity.defn(name=ActivityName.AUTHORIZE_PAYMENT)
    async def authorize_payment(order_id: str, price: PriceResult) -> str:
        calls.append(("authorize_payment", order_id))
        return outcome("authorize_payment", "ok")  # type: ignore[return-value]

    @activity.defn(name=ActivityName.CONFIRM_ORDER)
    async def confirm_order(order_id: str) -> None:
        calls.append(("confirm_order", order_id))
        outcome("confirm_order", None)

    @activity.defn(name=ActivityName.MARK_ACCEPTED)
    async def mark_accepted(order_id: str) -> None:
        calls.append(("mark_accepted", order_id))

    @activity.defn(name=ActivityName.MARK_PICKED_UP)
    async def mark_picked_up(order_id: str) -> None:
        calls.append(("mark_picked_up", order_id))

    @activity.defn(name=ActivityName.MARK_DELIVERED)
    async def mark_delivered(order_id: str) -> None:
        calls.append(("mark_delivered", order_id))

    @activity.defn(name=ActivityName.CAPTURE_PAYMENT)
    async def capture_payment(order_id: str) -> None:
        calls.append(("capture_payment", order_id))
        outcome("capture_payment", None)

    @activity.defn(name=ActivityName.SETTLE_ORDER)
    async def settle_order(order_id: str) -> None:
        calls.append(("settle_order", order_id))

    @activity.defn(name=ActivityName.BEGIN_CANCEL)
    async def begin_cancel(order_id: str, expected: str, reason: str) -> None:
        calls.append(("begin_cancel", order_id, expected, reason))

    @activity.defn(name=ActivityName.TRY_BEGIN_CANCEL)
    async def try_begin_cancel(order_id: str, reason: str) -> str:
        calls.append(("try_begin_cancel", order_id, reason))
        return outcome("try_begin_cancel", "ok")  # type: ignore[return-value]

    @activity.defn(name=ActivityName.VOID_AUTHORIZATION)
    async def void_authorization(order_id: str) -> None:
        calls.append(("void_authorization", order_id))

    @activity.defn(name=ActivityName.RELEASE_RESERVATION)
    async def release_reservation(order_id: str) -> None:
        calls.append(("release_reservation", order_id))
        outcome("release_reservation", None)

    @activity.defn(name=ActivityName.FINISH_CANCEL)
    async def finish_cancel(order_id: str, reason: str) -> None:
        calls.append(("finish_cancel", order_id, reason))

    @activity.defn(name=ActivityName.FIND_AND_OFFER)
    async def find_and_offer(order_id: str, attempt: int, exclude: list[str]) -> dict:
        calls.append(("find_and_offer", order_id, attempt, tuple(exclude)))
        return outcome(  # type: ignore[return-value]
            "find_and_offer",
            {"outcome": "offered", "offer_id": "off_test", "rider_id": "r_test", "timeout_s": 15.0},
        )

    @activity.defn(name=ActivityName.EXPIRE_OFFER)
    async def expire_offer(order_id: str, offer_id: str, rider_id: str) -> dict:
        calls.append(("expire_offer", order_id, offer_id, rider_id))
        return outcome("expire_offer", {"outcome": "revoked"})  # type: ignore[return-value]

    @activity.defn(name=ActivityName.UNASSIGN_STALLED)
    async def unassign_stalled(order_id: str, rider_id: str) -> dict:
        calls.append(("unassign_stalled", order_id, rider_id))
        return outcome("unassign_stalled", {"outcome": "revoked"})  # type: ignore[return-value]

    @activity.defn(name=ActivityName.CANCEL_DISPATCH)
    async def cancel_dispatch(order_id: str) -> dict:
        calls.append(("cancel_dispatch", order_id))
        return outcome("cancel_dispatch", {"outcome": "cancelled"})  # type: ignore[return-value]

    @activity.defn(name=ActivityName.RECORD_RIDER)
    async def record_rider(order_id: str, rider_id: str) -> None:
        calls.append(("record_rider", order_id, rider_id))

    return [
        create_order,
        validate_and_reserve,
        authorize_payment,
        confirm_order,
        mark_accepted,
        mark_picked_up,
        mark_delivered,
        capture_payment,
        settle_order,
        begin_cancel,
        try_begin_cancel,
        void_authorization,
        release_reservation,
        finish_cancel,
        find_and_offer,
        expire_offer,
        unassign_stalled,
        cancel_dispatch,
        record_rider,
    ], calls


async def _wait_for_child(env, order_id, *, attempts=200):
    """Block until dlv::{order_id} exists — the deterministic way to know
    the parent is inside the delivery window before we act on it."""
    handle = env.client.get_workflow_handle(f"dlv::{order_id}")
    for _ in range(attempts):
        try:
            await handle.describe()
            return
        except RPCError:
            await asyncio.sleep(0.05)
    raise AssertionError("DeliveryWorkflow never appeared")


async def _signal_food_ready(env, order_id, *, times=1):
    """The kitchen's cue, from outside: the child only exists once the
    parent processed the accept — retry NOT_FOUND until it appears."""
    handle = env.client.get_workflow_handle(f"dlv::{order_id}")
    for _ in range(200):
        try:
            for _ in range(times):
                await handle.signal("food_ready")
            return
        except RPCError:
            await asyncio.sleep(0.05)
    raise AssertionError("DeliveryWorkflow never appeared")


async def _drive_courier(env, order_id, *, offers=(("off_test", "r_test"),), deliver_as=None):
    """The rider's half of the happy path, from outside: accept the given
    offers, then pickup+deliver as the (last) rider. Signals are idempotent
    map/set writes in the child, so pre-sending them is deterministic —
    whenever the cascade reaches that offer, the answer is already there."""
    handle = env.client.get_workflow_handle(f"dlv::{order_id}")
    rider = deliver_as or offers[-1][1]
    for _ in range(200):
        try:
            for offer_id, rider_id in offers:
                await handle.signal("offer_accepted", args=[offer_id, rider_id])
            await handle.signal("courier_picked_up", rider)
            await handle.signal("courier_delivered", rider)
            return
        except RPCError:
            await asyncio.sleep(0.05)
    raise AssertionError("DeliveryWorkflow never appeared")


@asynccontextmanager
async def running_order(env, script=None, *, task_queue=None):
    """A live worker plus a freshly started OrderWorkflow, for tests that
    drive signals by hand: yields (handle, calls, order_id, task_queue).
    Both workflows run on the unsandboxed runner, and the start always
    carries accept_timeout_s=180."""
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = task_queue or f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities(script)
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with worker:
        handle = await env.client.start_workflow(
            "OrderWorkflow",
            WorkflowInput(placement=placement_for(order_id), accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        yield handle, calls, order_id, task_queue


async def _run(
    env,
    script=None,
    *,
    signal=None,
    cancel: bool | list[bool] = False,
    deliver=False,
    sandboxed=False,
    order_id=None,
    forward_deadline_s=300,
    knobs: dict | None = None,
):
    order_id = order_id or f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities(script)
    runner = SandboxedWorkflowRunner() if sandboxed else UnsandboxedWorkflowRunner()
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities,
        workflow_runner=runner,
    )
    async with worker:
        handle = await env.client.start_workflow(
            "OrderWorkflow",
            WorkflowInput(
                placement=placement_for(order_id),
                accept_timeout_s=180,
                forward_deadline_s=forward_deadline_s,
                **(knobs or {}),
            ),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        if cancel:
            for _ in cancel if isinstance(cancel, list) else [cancel]:
                await handle.signal("cancel_requested")
        if signal is not None:
            for verdict in signal if isinstance(signal, list) else [signal]:
                await handle.signal("restaurant_decision", verdict)
        if deliver:
            await _signal_food_ready(env, order_id)
            await _drive_courier(env, order_id)
        result = await handle.result()
    return result, calls, order_id


async def test_happy_path_runs_to_settled(env):
    result, calls, order_id = await _run(env, signal="accept", deliver=True)
    assert result == "SETTLED"
    assert [c[0] for c in calls] == [
        "create_order",
        "validate_and_reserve",
        "authorize_payment",
        "confirm_order",
        "mark_accepted",
        "find_and_offer",  # child: the cascade's one step (r_test accepts)
        "record_rider",  # the courier lands on the order row
        "mark_picked_up",  # child: the rider's pickup tap
        "mark_delivered",  # child: the rider's delivery tap
        "capture_payment",  # parent resumes: money becomes real
        "settle_order",  # reservation consumed, order closed
    ]


async def test_update_with_start_returns_the_ack_once_the_order_exists(env):
    """The placement handshake end to end, against a REAL Temporal server:
    one ExecuteMultiOperation call starts ord::{id} and blocks on
    await_placement, and comes back with the status create_order committed
    — the exact mechanism the POST route depends on (ADR-0023)."""
    from temporalio.client import WithStartWorkflowOperation
    from temporalio.common import WorkflowIDConflictPolicy

    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities()
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with worker:
        start = WithStartWorkflowOperation(
            "OrderWorkflow",
            WorkflowInput(placement=placement_for(order_id), accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        ack = await env.client.execute_update_with_start_workflow(
            UPDATE_AWAIT_PLACEMENT, start_workflow_operation=start, result_type=PlacementAck
        )
        assert ack == PlacementAck(order_id=order_id, status="PLACED")
        # It answered as soon as the order existed — NOT after the saga
        # finished: the workflow is still parked on the kitchen's decision.
        assert calls[0] == ("create_order", order_id)
        handle = env.client.get_workflow_handle(f"ord::{order_id}")
        assert (await handle.describe()).status.name == "RUNNING"
        await handle.signal("restaurant_decision", "reject")  # let it wind up
        assert await handle.result() == "CANCELLED"


async def test_a_retried_placement_attaches_to_the_running_saga(env):
    """A client retry (same key → same derived id) must join the workflow
    already in flight and receive the same ack — never fork a second
    order. USE_EXISTING is what makes that true."""
    from temporalio.client import WithStartWorkflowOperation
    from temporalio.common import WorkflowIDConflictPolicy

    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities()
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )

    def _start():
        return WithStartWorkflowOperation(
            "OrderWorkflow",
            WorkflowInput(placement=placement_for(order_id), accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

    async with worker:
        first = await env.client.execute_update_with_start_workflow(
            UPDATE_AWAIT_PLACEMENT, start_workflow_operation=_start(), result_type=PlacementAck
        )
        second = await env.client.execute_update_with_start_workflow(
            UPDATE_AWAIT_PLACEMENT, start_workflow_operation=_start(), result_type=PlacementAck
        )
        assert first == second
        # ONE workflow ran, so the order was created exactly once.
        assert [c[0] for c in calls].count("create_order") == 1
        handle = env.client.get_workflow_handle(f"ord::{order_id}")
        await handle.signal("restaurant_decision", "reject")
        await handle.result()


async def test_delivery_child_has_the_contract_id(env):
    _, _, order_id = await _run(env, signal="accept", deliver=True)
    child = await env.client.get_workflow_handle(f"dlv::{order_id}").describe()
    assert child.status is not None and child.status.name == "COMPLETED"


async def test_duplicate_food_ready_signals_are_noops(env):
    """A triple food_ready collapses into one truth — the courier leaves once."""
    async with running_order(env) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id, times=3)
        await _drive_courier(env, order_id)
        result = await handle.result()
    assert result == "SETTLED"
    names = [c[0] for c in calls]
    assert names.count("mark_picked_up") == 1 and names.count("mark_delivered") == 1


async def test_capture_conflict_fails_the_workflow_loudly(env):
    """Nothing-to-capture is a page, not a shrug: the non-retryable
    activity error fails the whole workflow and settle NEVER runs."""
    conflict = ApplicationError(
        "no capturable auth", non_retryable=True, type="PaymentStateConflict"
    )
    with pytest.raises(WorkflowFailureError):
        await _run(env, {"capture_payment": conflict}, signal="accept", deliver=True)


async def test_capture_transient_failure_retries_to_settled(env):
    result, calls, _ = await _run(
        env, {"capture_payment": [RuntimeError("psp blip"), None]}, signal="accept", deliver=True
    )
    assert result == "SETTLED"
    names = [c[0] for c in calls]
    assert names.count("capture_payment") == 2  # failed once, retried by policy
    assert names[-1] == "settle_order"


async def test_stock_failure_cancels_without_compensation(env):
    result, calls, _ = await _run(env, {"validate_and_reserve": "item_unavailable"})
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert "void_authorization" not in names and "release_reservation" not in names
    # begin_cancel names the expected state AND stamps the reason up front
    assert ("begin_cancel", calls[2][1], "PLACED", "item_unavailable") in calls
    assert calls[-1][2] == "item_unavailable"  # finish_cancel reason


async def test_capacity_failure_reason(env):
    result, calls, _ = await _run(env, {"validate_and_reserve": "at_capacity"})
    assert result == "CANCELLED"
    assert calls[-1][2] == "at_capacity"


async def test_decline_releases_stock_but_never_voids(env):
    result, calls, _ = await _run(env, {"authorize_payment": "declined"})
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert "release_reservation" in names
    assert "void_authorization" not in names  # §7 row 2: no auth to void
    assert calls[-1][2] == "payment_declined"


async def test_reject_unwinds_fully(env):
    result, calls, _ = await _run(env, signal="reject")
    names = [c[0] for c in calls]
    assert result == "CANCELLED"
    # reverse order of acquisition: cancel-mark, void, release, finish
    assert names[-4:] == [
        "begin_cancel",
        "void_authorization",
        "release_reservation",
        "finish_cancel",
    ]
    assert calls[-1][2] == "restaurant_rejected"


async def test_timeout_unwinds_fully(env):
    # No signal: the 180s timer fires (time-skipping makes it instant).
    result, calls, _ = await _run(env)
    assert result == "CANCELLED"
    assert calls[-1][2] == "restaurant_timeout"
    assert "void_authorization" in [c[0] for c in calls]


async def test_first_verdict_wins(env):
    result, calls, _ = await _run(env, signal=["accept", "reject"], deliver=True)
    assert result == "SETTLED"  # the late reject was a no-op


async def test_transient_activity_failure_retries_through(env):
    result, calls, _ = await _run(
        env,
        {"validate_and_reserve": [RuntimeError("inventory hiccup"), "ok"]},
        signal="accept",
        deliver=True,
    )
    assert result == "SETTLED"
    reserve_attempts = [c for c in calls if c[0] == "validate_and_reserve"]
    assert len(reserve_attempts) == 2  # failed once, retried by policy


async def test_duplicate_start_is_rejected(env):
    async with running_order(env) as (handle, _, order_id, task_queue):
        with pytest.raises(WorkflowAlreadyStartedError):
            await env.client.start_workflow(
                "OrderWorkflow",
                WorkflowInput(placement=placement_for(order_id), accept_timeout_s=180),
                id=f"ord::{order_id}",
                task_queue=task_queue,
            )
        await handle.signal("restaurant_decision", "reject")  # unwind: no child needed
        assert await handle.result() == "CANCELLED"


async def test_customer_cancel_before_any_verdict_unwinds_fully(env):
    result, calls, _ = await _run(env, cancel=True)
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    # pre-accept the customer always wins: known-state BEGIN_CANCEL, not TRY
    assert ("begin_cancel", calls[-4][1], "CONFIRMED", "customer_cancelled") in calls
    assert "try_begin_cancel" not in names
    assert names[-3:] == ["void_authorization", "release_reservation", "finish_cancel"]
    assert calls[-1][2] == "customer_cancelled"


async def test_customer_cancel_beats_a_simultaneous_accept(env):
    result, calls, _ = await _run(env, signal="accept", cancel=True)
    # both signals land before the wait wakes: cancel is checked first —
    # nothing is cooking yet, the customer wins
    assert result == "CANCELLED"
    assert "mark_accepted" not in [c[0] for c in calls]


async def test_customer_cancel_mid_kitchen_cancels_the_courier(env):
    async with running_order(env) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _wait_for_child(env, order_id)  # parent is now inside the delivery window
        await handle.signal("cancel_requested")  # kitchen never sends food_ready
        result = await handle.result()
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert names[-5:] == [
        "try_begin_cancel",  # the DB referees...
        "cancel_dispatch",  # ...the cancelled child frees dispatch...
        "void_authorization",  # ...then the §7 tail
        "release_reservation",
        "finish_cancel",
    ]
    assert "capture_payment" not in names and "settle_order" not in names
    child = await env.client.get_workflow_handle(f"dlv::{order_id}").describe()
    assert child.status is not None and child.status.name == "CANCELED"  # Temporal's spelling


async def test_customer_cancel_too_late_rides_to_settled(env):
    async with running_order(env, {"try_begin_cancel": "too_late"}) as (
        handle,
        calls,
        order_id,
        _,
    ):
        await handle.signal("restaurant_decision", "accept")
        await _wait_for_child(env, order_id)
        await handle.signal("cancel_requested")  # courier "already picked up"
        await _signal_food_ready(env, order_id)  # delivery continues regardless
        await _drive_courier(env, order_id)
        result = await handle.result()
    assert result == "SETTLED"
    names = [c[0] for c in calls]
    assert names.count("try_begin_cancel") == 1  # asked once, told no
    assert "begin_cancel" not in names and "finish_cancel" not in names
    assert names[-2:] == ["capture_payment", "settle_order"]  # order completed normally


async def test_cancel_completes_even_if_the_child_fails_on_the_guard(env):
    """The drain invariant: a won cancel flips the row to CANCELLING; if the
    child's mark then races in, it fails on the guard instead of ending
    cleanly cancelled — EITHER ending must leave the unwind completing.
    (mark_picked_up is scripted to fail as the guard would; whether this
    run's child dies on it or is cancelled first, CANCELLED must result.)"""
    guard = ApplicationError("order is CANCELLING", non_retryable=True, type="IllegalTransition")
    async with running_order(env, {"mark_picked_up": guard}) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)  # child heads into its cascade
        await _drive_courier(env, order_id)  # rider accepts+taps — mark will hit the guard
        await handle.signal("cancel_requested")
        result = await handle.result()
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert names[-3:] == ["void_authorization", "release_reservation", "finish_cancel"]
    assert "capture_payment" not in names


async def test_duplicate_cancel_signals_unwind_once(env):
    result, calls, _ = await _run(env, cancel=[True, True])
    assert result == "CANCELLED"
    assert [c[0] for c in calls].count("finish_cancel") == 1


async def test_workflow_survives_the_real_sandbox(env):
    """Import purity: the default sandboxed runner re-imports workflows.py;
    heavyweight imports (sqlalchemy/httpx) would fail right here — and the
    accept path drives DeliveryWorkflow through the same sandbox."""
    result, _, _ = await _run(env, signal="accept", deliver=True, sandboxed=True)
    assert result == "SETTLED"


async def test_build_worker_registers_the_full_surface(env):
    """The worker composition seam: real OrderActivities wired end to end
    through build_worker and driven by the real workflow."""
    from order.activities import OrderActivities
    from order.db import metadata
    from order.worker import build_worker
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    class NullClient:
        async def reserve(self, **kwargs):
            return "ok"  # pragma: no cover — never driven here

    activities = OrderActivities(
        sessions,
        NullClient(),  # type: ignore[arg-type]
        NullClient(),  # type: ignore[arg-type]
        NullClient(),  # type: ignore[arg-type]
    )
    worker = build_worker(env.client, activities, task_queue="tq-build-test")
    assert worker.task_queue == "tq-build-test"


# ── forward deadlines (compensations stay unbounded) ───────────────


async def test_reserve_deadline_cancels_and_releases_without_voiding(env):
    """Inventory unreachable past the deadline. The reserve MAY have landed
    on an attempt whose answer was lost, so the unwind releases; nothing
    could have been authorized yet, so it must NOT void."""
    result, calls, _ = await _run(
        env,
        {"validate_and_reserve": RuntimeError("inventory unreachable")},
        forward_deadline_s=60,
    )
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert "release_reservation" in names
    assert "void_authorization" not in names  # no hold could exist yet
    begin = next(c for c in calls if c[0] == "begin_cancel")
    assert begin[2] == "PLACED" and begin[3] == "system_timeout"


async def test_authorize_deadline_voids_and_releases(env):
    """The PSP stayed unreachable: an authorization may have gone through on
    a lost attempt, so the unwind voids AND releases — both idempotent."""
    result, calls, _ = await _run(
        env,
        {"authorize_payment": RuntimeError("psp unreachable")},
        forward_deadline_s=60,
    )
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert names.index("void_authorization") < names.index("release_reservation")  # §7 order
    begin = next(c for c in calls if c[0] == "begin_cancel")
    assert begin[2] == "VALIDATED" and begin[3] == "system_timeout"


async def test_confirm_deadline_unwinds_from_payment_cleared(env):
    result, calls, _ = await _run(
        env,
        {"confirm_order": RuntimeError("db unreachable")},
        forward_deadline_s=60,
    )
    assert result == "CANCELLED"
    begin = next(c for c in calls if c[0] == "begin_cancel")
    assert begin[2] == "PAYMENT_CLEARED" and begin[3] == "system_timeout"
    names = [c[0] for c in calls]
    assert "void_authorization" in names and "release_reservation" in names


async def test_compensations_are_not_bounded_by_the_forward_deadline(env):
    """THE point of splitting the policy: the unwind may take far longer
    than the forward deadline and must still finish. Here the release keeps
    failing for ~31s of retry backoff against a 30s forward deadline — if
    compensations shared that deadline, this order would never reach
    CANCELLED and the stock would stay held."""
    result, calls, _ = await _run(
        env,
        {
            "authorize_payment": RuntimeError("psp unreachable"),
            "release_reservation": [
                RuntimeError("inventory down"),
                RuntimeError("inventory down"),
                RuntimeError("inventory down"),
                RuntimeError("inventory down"),
                RuntimeError("inventory down"),
                None,
            ],
        },
        forward_deadline_s=30,
    )
    assert result == "CANCELLED"
    assert [c[0] for c in calls].count("release_reservation") == 6  # 5 failures, then it lands
    assert calls[-1][0] == "finish_cancel"


async def test_a_non_retryable_forward_failure_still_fails_the_workflow(env):
    """The other side of the discriminator: an IllegalTransition means the
    world disagrees with our history. Retrying cannot fix it and cancelling
    would be guessing — it must stay a loud workflow failure, not become a
    system_timeout cancel."""
    guard = ApplicationError("order is not PLACED", non_retryable=True, type="IllegalTransition")
    with pytest.raises(WorkflowFailureError):
        await _run(env, {"validate_and_reserve": guard}, forward_deadline_s=60)


# ── the dispatch cascade (the milestone's new machinery) ───────────


async def test_cascade_moves_to_the_second_candidate(env):
    """FR-29: the first rider ignores the 15s window — expire revokes,
    the cascade excludes them and courts the next, who accepts."""
    script = {
        "find_and_offer": [
            {"outcome": "offered", "offer_id": "off_1", "rider_id": "r_1", "timeout_s": 15.0},
            {"outcome": "offered", "offer_id": "off_2", "rider_id": "r_2", "timeout_s": 12.0},
        ]
    }
    async with running_order(env, script) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)
        await _drive_courier(env, order_id, offers=(("off_2", "r_2"),))  # r_1 never answers
        result = await handle.result()
    assert result == "SETTLED"
    assert ("expire_offer", order_id, "off_1", "r_1") in calls
    assert ("record_rider", order_id, "r_2") in calls
    finds = [c for c in calls if c[0] == "find_and_offer"]
    assert finds[0][2] == 1 and finds[0][3] == ()
    assert finds[1][2] == 2 and finds[1][3] == ("r_1",)  # the ghost is excluded


async def test_lost_accept_signal_selfheals_via_the_expire_read(env):
    """The race matrix's crown case: the accept converted DDB but its
    signal never arrived. The expiry activity reads the truth and the
    workflow proceeds with the rider it was never told about."""
    script = {"expire_offer": {"outcome": "already_assigned", "rider_id": "r_test"}}
    async with running_order(env, script) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)
        # No offer_accepted signal AT ALL — only the rider's later taps.
        await _drive_courier(env, order_id, offers=(), deliver_as="r_test")
        result = await handle.result()
    assert result == "SETTLED"
    names = [c[0] for c in calls]
    assert names.count("find_and_offer") == 1  # no second cascade step
    assert ("record_rider", order_id, "r_test") in calls


async def test_empty_city_cancels_after_the_deadline(env):
    """FR-32: cooked, READY, and nobody to carry it. The child answers
    NO_RIDER and the PARENT cancels through the normal §7 unwind with the
    new reason — customer refunded (void), stock released."""
    script = {"find_and_offer": {"outcome": "no_candidates"}}
    async with running_order(env, script) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)
        result = await handle.result()
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert names.count("find_and_offer") >= 2  # it kept trying to the deadline
    assert ("try_begin_cancel", order_id, "no_rider_available") in calls
    assert "cancel_dispatch" in names
    assert names[-3:] == ["void_authorization", "release_reservation", "finish_cancel"]
    assert calls[-1][2] == "no_rider_available"


async def test_ghost_rider_is_revoked_and_replaced(env):
    """FR-30: accepted but never picked up. The pickup deadline revokes
    (conditional — ADR-0011), the cascade resumes without the ghost, and
    the ghost's own late signals can never advance the NEW courier."""
    script = {
        "find_and_offer": [
            {"outcome": "offered", "offer_id": "off_1", "rider_id": "r_1", "timeout_s": 15.0},
            {"outcome": "offered", "offer_id": "off_2", "rider_id": "r_2", "timeout_s": 12.0},
        ]
    }
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities(script)
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with worker:
        handle = await env.client.start_workflow(
            "OrderWorkflow",
            WorkflowInput(
                placement=placement_for(order_id), accept_timeout_s=180, pickup_timeout_s=30.0
            ),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)
        # Both riders accept their offers, but ONLY r_2 ever picks up.
        await _drive_courier(
            env, order_id, offers=(("off_1", "r_1"), ("off_2", "r_2")), deliver_as="r_2"
        )
        result = await handle.result()
    assert result == "SETTLED"
    assert ("unassign_stalled", order_id, "r_1") in calls
    riders = [c[2] for c in calls if c[0] == "record_rider"]
    assert riders == ["r_1", "r_2"]  # stamped, revoked, restamped — truth in order
    assert [c[0] for c in calls].count("mark_picked_up") == 1  # the ghost never marked


async def test_ghost_pickup_beats_the_revoke_and_rides_on(env):
    """ADR-0011's revoke rule, workflow-side: the deadline fired, but the
    conditional unassign found a completed pickup — the 'ghost' was merely
    slow, and the delivery proceeds with them."""
    script = {"unassign_stalled": {"outcome": "already_picked_up"}}
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities(script)
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OrderWorkflow, DeliveryWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with worker:
        handle = await env.client.start_workflow(
            "OrderWorkflow",
            WorkflowInput(
                placement=placement_for(order_id), accept_timeout_s=180, pickup_timeout_s=30.0
            ),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)
        child = env.client.get_workflow_handle(f"dlv::{order_id}")
        for _ in range(200):  # accept, but never send the pickup signal
            try:
                await child.signal("offer_accepted", args=["off_test", "r_test"])
                await child.signal("courier_delivered", "r_test")
                break
            except RPCError:
                await asyncio.sleep(0.05)
        result = await handle.result()
    assert result == "SETTLED"
    names = [c[0] for c in calls]
    assert names.index("unassign_stalled") < names.index("mark_picked_up")
    assert names.count("find_and_offer") == 1  # no re-cascade — the rider was real


async def test_missed_riders_get_a_fresh_round_after_an_empty_search(env):
    """Found live: in a one-rider town, one missed offer put the only
    courier on the exclude list forever. An empty search now clears the
    exclusions after the breather — the same rider is courted again."""
    script = {
        "find_and_offer": [
            {"outcome": "offered", "offer_id": "off_1", "rider_id": "r_1", "timeout_s": 15.0},
            {"outcome": "no_candidates"},  # everyone excluded — the empty round
            {"outcome": "offered", "offer_id": "off_2", "rider_id": "r_1", "timeout_s": 12.0},
        ]
    }
    async with running_order(env, script) as (handle, calls, order_id, _):
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)
        await _drive_courier(env, order_id, offers=(("off_2", "r_1"),))  # miss off_1, take off_2
        result = await handle.result()
    assert result == "SETTLED"
    finds = [c for c in calls if c[0] == "find_and_offer"]
    assert finds[1][3] == ("r_1",)  # excluded after the miss...
    assert finds[2][3] == ()  # ...and welcomed back after the empty round
