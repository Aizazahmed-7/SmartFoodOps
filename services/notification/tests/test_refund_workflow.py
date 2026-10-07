"""RefundNotificationWorkflow end to end (ADR-0040).

Workflow logic runs against Temporal's time-skipping test server with MOCK
activities registered under the same names (the order suite's idiom — the
real activities have their own sqlite/httpx tests below), and the
unsandboxed runner so coverage traces workflow code.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from notification.values import ActivityName, RefundNotice
from notification.workflows import RefundNotificationWorkflow
from temporalio import activity
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

NOTICE = RefundNotice(
    event_id="evt-r1",
    order_id="ord_1",
    amount_cents=1200,
    currency="USD",
    occurred_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat(),
)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


def mock_activities(*, lookup: object = "usr_1"):
    """`lookup` may be a value, an Exception, or a list popped per call."""
    calls: list[tuple] = []
    script = lookup if isinstance(lookup, list) else [lookup]
    remaining = list(script)

    @activity.defn(name=ActivityName.FETCH_RECIPIENTS)
    async def fetch_recipients(order_id: str) -> str:
        calls.append(("fetch", order_id))
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, Exception):
            raise value
        return str(value)

    @activity.defn(name=ActivityName.WRITE_NOTIFICATION)
    async def write_notification(notice: RefundNotice, user_id: str) -> None:
        calls.append(("write", notice.order_id, user_id))

    return [fetch_recipients, write_notification], calls


async def _run(env: WorkflowEnvironment, acts, *, lookup_budget: float = 3600.0) -> str:
    async with Worker(
        env.client,
        task_queue=f"tq-{uuid.uuid4().hex}",
        workflows=[RefundNotificationWorkflow],
        activities=acts,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ) as worker:
        return await env.client.execute_workflow(
            RefundNotificationWorkflow.run,
            args=[NOTICE, lookup_budget],
            id=f"ntf::refund::{uuid.uuid4().hex}",
            task_queue=worker.task_queue,
        )


async def test_happy_path_looks_up_then_writes(env: WorkflowEnvironment):
    acts, calls = mock_activities()
    assert await _run(env, acts) == "usr_1"
    assert calls == [("fetch", "ord_1"), ("write", "ord_1", "usr_1")]


async def test_an_order_outage_is_ridden_out_not_dropped(env: WorkflowEnvironment):
    """The whole point of the workflow: order being down delays the bell
    entry, it does not lose it. Two failures, then success — and the write
    still happens."""
    acts, calls = mock_activities(
        lookup=[ConnectionError("order down"), ConnectionError("still down"), "usr_1"]
    )
    assert await _run(env, acts) == "usr_1"
    assert [c[0] for c in calls] == ["fetch", "fetch", "fetch", "write"]


async def test_an_unknown_order_fails_fast_instead_of_polling_for_an_hour(
    env: WorkflowEnvironment,
):
    """A 404 will never become a 200. Retrying it to the schedule_to_close
    horizon would hold a workflow open for an hour to learn nothing."""
    acts, calls = mock_activities(
        lookup=ApplicationError("no such order: ord_1", non_retryable=True)
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011 — Temporal wraps it
        await _run(env, acts)
    assert isinstance(exc.value.__cause__, ActivityError)
    assert [c[0] for c in calls] == ["fetch"]  # exactly one attempt, no write


async def test_the_lookup_budget_bounds_the_whole_retry_chain(env: WorkflowEnvironment):
    """schedule_to_close, not start_to_close: a permanently unreachable
    order gives up at the budget rather than retrying forever."""
    acts, calls = mock_activities(lookup=ConnectionError("order down"))
    with pytest.raises(Exception):  # noqa: B017, PT011 — timeout surfaces wrapped
        await _run(env, acts, lookup_budget=5.0)
    assert ("write", "ord_1", "usr_1") not in calls  # never wrote a half-truth
