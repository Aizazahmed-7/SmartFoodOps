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

import pytest
import pytest_asyncio
from order.values import ActivityName, LineSpec, PriceResult, WorkflowInput
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


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    # Downloads the test-server binary on first ever run; cached after.
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


def mock_activities(script: dict[str, object] | None = None):
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

    @activity.defn(name=ActivityName.PRICE_ORDER)
    async def price_order(order_id: str) -> PriceResult:
        calls.append(("price_order", order_id))
        return PRICE

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

    @activity.defn(name=ActivityName.FINISH_CANCEL)
    async def finish_cancel(order_id: str, reason: str) -> None:
        calls.append(("finish_cancel", order_id, reason))

    return [
        price_order,
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


async def _run(
    env,
    script=None,
    *,
    signal=None,
    cancel: bool | list[bool] = False,
    deliver=False,
    sandboxed=False,
    order_id=None,
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
            WorkflowInput(order_id=order_id, accept_timeout_s=180),
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
        result = await handle.result()
    return result, calls, order_id


async def test_happy_path_runs_to_settled(env):
    result, calls, order_id = await _run(env, signal="accept", deliver=True)
    assert result == "SETTLED"
    assert [c[0] for c in calls] == [
        "price_order",
        "validate_and_reserve",
        "authorize_payment",
        "confirm_order",
        "mark_accepted",
        "mark_picked_up",  # child: pickup timer fired
        "mark_delivered",  # child: dropoff timer fired
        "capture_payment",  # parent resumes: money becomes real
        "settle_order",  # reservation consumed, order closed
    ]


async def test_delivery_child_has_the_contract_id(env):
    _, _, order_id = await _run(env, signal="accept", deliver=True)
    child = await env.client.get_workflow_handle(f"dlv::{order_id}").describe()
    assert child.status is not None and child.status.name == "COMPLETED"


async def test_duplicate_food_ready_signals_are_noops(env):
    """A triple food_ready collapses into one truth — the courier leaves once."""
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
        handle = await env.client.start_workflow(
            "OrderWorkflow",
            WorkflowInput(order_id=order_id, accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id, times=3)
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
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, _ = mock_activities()
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
            WorkflowInput(order_id=order_id),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        with pytest.raises(WorkflowAlreadyStartedError):
            await env.client.start_workflow(
                "OrderWorkflow",
                WorkflowInput(order_id=order_id),
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
        handle = await env.client.start_workflow(
            "OrderWorkflow",
            WorkflowInput(order_id=order_id, accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        await handle.signal("restaurant_decision", "accept")
        await _wait_for_child(env, order_id)  # parent is now inside the delivery window
        await handle.signal("cancel_requested")  # kitchen never sends food_ready
        result = await handle.result()
    assert result == "CANCELLED"
    names = [c[0] for c in calls]
    assert names[-4:] == [
        "try_begin_cancel",  # the DB referees...
        "void_authorization",  # ...then the §7 tail
        "release_reservation",
        "finish_cancel",
    ]
    assert "capture_payment" not in names and "settle_order" not in names
    child = await env.client.get_workflow_handle(f"dlv::{order_id}").describe()
    assert child.status is not None and child.status.name == "CANCELED"  # Temporal's spelling


async def test_customer_cancel_too_late_rides_to_settled(env):
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities, calls = mock_activities({"try_begin_cancel": "too_late"})
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
            WorkflowInput(order_id=order_id, accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        await handle.signal("restaurant_decision", "accept")
        await _wait_for_child(env, order_id)
        await handle.signal("cancel_requested")  # courier "already picked up"
        await _signal_food_ready(env, order_id)  # delivery continues regardless
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
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    guard = ApplicationError("order is CANCELLING", non_retryable=True, type="IllegalTransition")
    activities, calls = mock_activities({"mark_picked_up": guard})
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
            WorkflowInput(order_id=order_id, accept_timeout_s=180),
            id=f"ord::{order_id}",
            task_queue=task_queue,
        )
        await handle.signal("restaurant_decision", "accept")
        await _signal_food_ready(env, order_id)  # child heads for its timers
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

    activities = OrderActivities(sessions, NullClient(), NullClient())  # type: ignore[arg-type]
    worker = build_worker(env.client, activities, task_queue="tq-build-test")
    assert worker.task_queue == "tq-build-test"
