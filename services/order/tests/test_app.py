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


async def test_temporal_saga_starts_by_name_and_swallows_duplicates():
    from order.adapters.temporal_client import TemporalSaga
    from order.values import WorkflowInput
    from temporalio.exceptions import WorkflowAlreadyStartedError

    class FakeClient:
        def __init__(self):
            self.calls: list = []

        async def start_workflow(self, name, arg, *, id, task_queue, id_reuse_policy):
            self.calls.append((name, arg, id, task_queue))
            if len(self.calls) > 1:
                raise WorkflowAlreadyStartedError(id, name)

    fake = FakeClient()
    saga = TemporalSaga(
        "unused:7233",
        task_queue="order-tq",
        accept_timeout_s=180,
        pickup_delay_s=20,
        dropoff_delay_s=30,
        client=fake,  # type: ignore[arg-type]
    )
    await saga.start("ord_1")
    await saga.start("ord_1")  # duplicate — swallowed, not raised
    name, arg, workflow_id, task_queue = fake.calls[0]
    assert name == "OrderWorkflow"
    assert isinstance(arg, WorkflowInput) and arg.order_id == "ord_1"
    assert (workflow_id, task_queue) == ("ord::ord_1", "order-tq")
    assert len(fake.calls) == 2


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
