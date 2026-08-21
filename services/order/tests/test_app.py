"""App wiring: healthz, real-adapter construction, lifespan cleanup."""

from fastapi.testclient import TestClient
from order.config import Settings
from order.main import create_app


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "order"}


def test_app_builds_real_catalog_client_when_none_injected():
    """No catalog injected → the app constructs the real adapter and owns
    its http client's lifecycle (closed on shutdown without error)."""
    app = create_app(Settings(database_url="sqlite+aiosqlite://", create_all=True))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
    # exiting the context ran the lifespan shutdown — own_http.aclose()


def test_worker_module_importable():
    """The worker module wires without a Temporal server present."""
    from order import worker

    assert callable(worker.main) and callable(worker.build_worker)


def _placement(order_id="ord_1"):
    from order.values import PlacementInput

    return PlacementInput(
        order_id=order_id,
        request_hash="hash-of-K-1",
        user_id="usr_1",
        restaurant_id="rst_1",
        restaurant_name="Biryani House",
        card_token="tok_ok",
        menu_version=3,
        currency="USD",
        amount_cents=1500,
        placed_at="2026-08-18T10:00:00+00:00",
    )


def _started(operation):
    """The start half of an update-with-start, as the SDK recorded it.
    Private attribute on purpose: there is no public accessor, and the id
    and policies are exactly the contract worth pinning."""
    return operation._start_workflow_input


def _place_saga(*, answer=None, raises=None):
    """A TemporalSaga whose one RPC either returns `answer` or raises."""
    from order.adapters.temporal_client import TemporalSaga

    calls: list = []

    class FakeClient:
        async def execute_update_with_start_workflow(
            self, update, *, start_workflow_operation, result_type, rpc_timeout
        ):
            calls.append((update, start_workflow_operation, rpc_timeout))
            if raises is not None:
                raise raises
            return answer

    saga = TemporalSaga(
        "unused:7233",
        task_queue="order-tq",
        accept_timeout_s=180,
        pickup_delay_s=20,
        dropoff_delay_s=30,
        await_seconds=2.0,
        client=FakeClient(),  # type: ignore[arg-type]
    )
    return saga, calls


async def test_place_sends_start_and_update_as_one_call():
    """The id contracts ADR-0023 rests on: workflow id ord::{order_id},
    the placement rides in the START operation, and the update we await is
    await_placement — one RPC, so a started workflow can never be left
    without the request that is waiting on it."""
    from datetime import timedelta

    from order.values import UPDATE_AWAIT_PLACEMENT, PlacementAck, WorkflowInput

    saga, calls = _place_saga(answer=PlacementAck(order_id="ord_1", status="PLACED"))
    ack = await saga.place(_placement())

    assert ack == PlacementAck(order_id="ord_1", status="PLACED")
    update, start, rpc_timeout = calls[0]
    assert update == UPDATE_AWAIT_PLACEMENT
    started = _started(start)
    assert started.id == "ord::ord_1" and started.task_queue == "order-tq"
    assert isinstance(started.args[0], WorkflowInput)
    assert started.args[0].placement.order_id == "ord_1"
    assert rpc_timeout == timedelta(seconds=2.0)


async def test_place_reuse_and_conflict_policies_are_pinned():
    """USE_EXISTING (running → attach) and REJECT_DUPLICATE (closed →
    refuse) answer different questions; swapping either one silently
    changes what a retried placement does."""
    from order.values import PlacementAck
    from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

    saga, calls = _place_saga(answer=PlacementAck(order_id="ord_1", status="PLACED"))
    await saga.place(_placement())
    started = _started(calls[0][1])
    assert started.id_conflict_policy == WorkflowIDConflictPolicy.USE_EXISTING
    assert started.id_reuse_policy == WorkflowIDReusePolicy.REJECT_DUPLICATE


async def test_place_timeouts_are_pending_not_failures():
    """Both flavours of "we stopped waiting" — the SDK's update-timeout and
    a raw DEADLINE_EXCEEDED — mean the workflow is still running. Answering
    anything but PlacementPending here would invite a duplicate order."""
    from order.domain.ports import PlacementPending
    from temporalio.client import WorkflowUpdateRPCTimeoutOrCancelledError
    from temporalio.service import RPCError, RPCStatusCode

    for failure in (
        WorkflowUpdateRPCTimeoutOrCancelledError(),
        RPCError("deadline", RPCStatusCode.DEADLINE_EXCEEDED, b""),
    ):
        saga, _ = _place_saga(raises=failure)
        assert await saga.place(_placement()) == PlacementPending("ord_1")


async def test_place_on_a_closed_workflow_asks_the_domain():
    """REJECT_DUPLICATE refusing a finished execution is not an outage —
    it is a reused key pointing at an order that already ran. The adapter
    has no database, so it names the case and the domain answers it."""
    import pytest
    from order.domain.ports import SagaClosed
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.service import RPCError, RPCStatusCode

    for failure in (
        WorkflowAlreadyStartedError("ord::ord_1", "OrderWorkflow"),
        RPCError("exists", RPCStatusCode.ALREADY_EXISTS, b""),
    ):
        saga, _ = _place_saga(raises=failure)
        with pytest.raises(SagaClosed):
            await saga.place(_placement())


async def test_place_update_failure_is_saga_unavailable_not_a_500():
    """If create_order ever gains a non-retryable failure, the pending
    update fails. That must reach the customer as the domain's 503 —
    "try again" — never as a raw exception the route turns into a 500."""
    import pytest
    from order.domain.ports import SagaUnavailable
    from temporalio.client import WorkflowUpdateFailedError

    saga, _ = _place_saga(raises=WorkflowUpdateFailedError(RuntimeError("activity failed")))
    with pytest.raises(SagaUnavailable):
        await saga.place(_placement())


def _attach_saga(*, answer=None, raises=None):
    """A TemporalSaga whose update-only RPC either returns or raises."""
    from order.adapters.temporal_client import TemporalSaga

    calls: list = []

    class FakeHandle:
        def __init__(self, workflow_id):
            self._id = workflow_id

        async def execute_update(self, update, *, result_type, rpc_timeout):
            calls.append((self._id, update, rpc_timeout))
            if raises is not None:
                raise raises
            return answer

    class FakeClient:
        def get_workflow_handle(self, workflow_id):
            return FakeHandle(workflow_id)

    saga = TemporalSaga(
        "unused:7233",
        task_queue="order-tq",
        accept_timeout_s=180,
        pickup_delay_s=20,
        dropoff_delay_s=30,
        await_seconds=2.0,
        client=FakeClient(),  # type: ignore[arg-type]
    )
    return saga, calls


async def test_attach_placement_awaits_the_update_without_starting():
    """The ADR-0024 probe: update-only against ord::{id}, same await
    budget as place(), no WithStart anywhere near it."""
    from datetime import timedelta

    from order.values import UPDATE_AWAIT_PLACEMENT, PlacementAck

    saga, calls = _attach_saga(answer=PlacementAck(order_id="ord_1", status="PLACED"))
    ack = await saga.attach_placement("ord_1")
    assert ack == PlacementAck(order_id="ord_1", status="PLACED")
    assert calls == [("ord::ord_1", UPDATE_AWAIT_PLACEMENT, timedelta(seconds=2.0))]


async def test_attach_placement_maps_every_failure_to_a_domain_answer():
    """NOT_FOUND (no workflow, or it finished) → SagaGone; both timeout
    flavours → pending (the workflow is real and slow); update-failed and
    transport trouble → SagaUnavailable."""
    import pytest
    from order.domain.ports import PlacementPending, SagaGone, SagaUnavailable
    from temporalio.client import (
        WorkflowUpdateFailedError,
        WorkflowUpdateRPCTimeoutOrCancelledError,
    )
    from temporalio.service import RPCError, RPCStatusCode

    saga, _ = _attach_saga(raises=RPCError("no workflow", RPCStatusCode.NOT_FOUND, b""))
    with pytest.raises(SagaGone):
        await saga.attach_placement("ord_1")

    for timeout in (
        WorkflowUpdateRPCTimeoutOrCancelledError(),
        RPCError("deadline", RPCStatusCode.DEADLINE_EXCEEDED, b""),
    ):
        saga, _ = _attach_saga(raises=timeout)
        assert await saga.attach_placement("ord_1") == PlacementPending("ord_1")

    for hard in (
        WorkflowUpdateFailedError(RuntimeError("activity failed")),
        RPCError("conn refused", RPCStatusCode.UNAVAILABLE, b""),
        OSError("dns says no"),
    ):
        saga, _ = _attach_saga(raises=hard)
        with pytest.raises(SagaUnavailable):
            await saga.attach_placement("ord_1")


async def test_place_transport_failures_are_saga_unavailable():
    """Temporal is on the checkout path now: its outage must arrive as the
    domain's 503, never as a raw exception (a 500 would tell the customer
    to re-order)."""
    import pytest
    from order.domain.ports import SagaUnavailable
    from temporalio.service import RPCError, RPCStatusCode

    for failure in (
        RPCError("conn refused", RPCStatusCode.UNAVAILABLE, b""),
        OSError("dns says no"),
    ):
        saga, _ = _place_saga(raises=failure)
        with pytest.raises(SagaUnavailable):
            await saga.place(_placement())


def _signal_saga(fail_with=None):
    from order.adapters.temporal_client import TemporalSaga

    signals: list = []

    class FakeHandle:
        def __init__(self, workflow_id):
            self._id = workflow_id

        async def signal(self, name, *args):
            if fail_with is not None:
                raise fail_with
            signals.append((self._id, name, args))

    class FakeClient:
        def get_workflow_handle(self, workflow_id):
            return FakeHandle(workflow_id)

    saga = TemporalSaga(
        "unused:7233",
        task_queue="order-tq",
        accept_timeout_s=180,
        pickup_delay_s=20,
        dropoff_delay_s=30,
        client=FakeClient(),  # type: ignore[arg-type]
    )
    return saga, signals


async def test_saga_signals_target_the_id_contracts():
    saga, signals = _signal_saga()
    await saga.signal_decision("ord_1", "accept")
    await saga.signal_food_ready("ord_1")
    await saga.signal_cancel("ord_1")
    assert signals == [
        ("ord::ord_1", "restaurant_decision", ("accept",)),
        ("dlv::ord_1", "food_ready", ()),
        ("ord::ord_1", "cancel_requested", ()),
    ]


async def test_saga_signal_rpc_errors_become_domain_answers():
    import pytest
    from order.domain.ports import SagaGone, SagaUnavailable
    from temporalio.service import RPCError, RPCStatusCode

    saga, _ = _signal_saga(RPCError("already completed", RPCStatusCode.NOT_FOUND, b""))
    with pytest.raises(SagaGone):
        await saga.signal_decision("ord_1", "accept")

    saga, _ = _signal_saga(RPCError("conn refused", RPCStatusCode.UNAVAILABLE, b""))
    with pytest.raises(SagaUnavailable):
        await saga.signal_food_ready("ord_1")


async def test_saga_signal_connect_failure_is_503_not_500(monkeypatch):
    """The lazy first connect lives INSIDE the domain mapping: a signal
    while Temporal is down must reach the kitchen as SagaUnavailable
    (-> 503 Retry-After), never a raw exception (-> 500)."""
    import pytest
    from order.adapters import temporal_client
    from order.adapters.temporal_client import TemporalSaga
    from order.domain.ports import SagaUnavailable
    from temporalio.service import RPCError, RPCStatusCode

    for refusal in (
        RPCError("connect refused", RPCStatusCode.UNAVAILABLE, b""),
        OSError("dns says no"),  # socket-layer failure below the RPC layer
    ):

        async def refuse(address, *, _exc=refusal, **kwargs):
            raise _exc

        monkeypatch.setattr(temporal_client.Client, "connect", refuse)
        saga = TemporalSaga(
            "down:7233",
            task_queue="order-tq",
            accept_timeout_s=180,
            pickup_delay_s=20,
            dropoff_delay_s=30,
        )
        with pytest.raises(SagaUnavailable):
            await saga.signal_decision("ord_1", "accept")


def test_injected_poller_lives_and_dies_with_the_app():
    import asyncio

    class StubPoller:
        def __init__(self):
            self.started = False
            self.cancelled = False

        async def run(self):
            self.started = True
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    poller = StubPoller()
    app = create_app(
        Settings(database_url="sqlite+aiosqlite://", create_all=True),
        poller=poller,  # type: ignore[arg-type]
    )
    with TestClient(app):
        pass
    assert poller.started and poller.cancelled
