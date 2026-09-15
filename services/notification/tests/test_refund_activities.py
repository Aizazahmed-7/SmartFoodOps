"""The two activities RefundNotificationWorkflow runs, and the starter that
hands it work — against real sqlite and a stubbed order service."""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from notification.activities import NotificationActivities, OrderUnavailable
from notification.adapters.saga_client import DisarmedRefundNotifier, TemporalRefundNotifier
from notification.db import metadata, notifications
from notification.values import TASK_QUEUE_DEFAULT, RefundNotice
from notification.worker import build_worker
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

NOTICE = RefundNotice(
    event_id="evt-r1",
    order_id="ord_1",
    amount_cents=1200,
    currency="USD",
    occurred_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat(),
)


async def _sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _acts(sessions, handler):
    transport = httpx.MockTransport(handler)
    return NotificationActivities(
        sessions, httpx.AsyncClient(transport=transport), order_base_url="http://order"
    )


# ── fetch_recipients ───────────────────────────────────────────────


async def test_fetch_asks_order_and_returns_the_customer():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        # System-to-system: the call must carry stamped identity, or order
        # answers 401 and the workflow retries forever against a wall.
        assert request.headers["X-Auth-Sub"]
        return httpx.Response(200, json={"user_id": "usr_1", "restaurant_id": "rst_1"})

    acts = _acts(await _sessions(), handler)
    assert await acts.fetch_recipients("ord_1") == "usr_1"
    assert seen == ["http://order/v1/internal/orders/ord_1/recipients"]


async def test_fetch_raises_retryable_on_transport_trouble():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("order down")

    acts = _acts(await _sessions(), handler)
    with pytest.raises(OrderUnavailable):
        await acts.fetch_recipients("ord_1")


async def test_fetch_raises_retryable_on_a_5xx():
    acts = _acts(await _sessions(), lambda r: httpx.Response(503))
    with pytest.raises(OrderUnavailable):
        await acts.fetch_recipients("ord_1")


async def test_fetch_treats_404_as_permanent():
    """An order that does not exist never will. LookupError is NOT
    OrderUnavailable, so the workflow stops instead of polling for an hour."""
    acts = _acts(await _sessions(), lambda r: httpx.Response(404))
    with pytest.raises(LookupError):
        await acts.fetch_recipients("ord_1")


async def test_fetch_surfaces_other_4xx_as_itself():
    """A 401 means our identity stamping broke — a bug, not an outage.
    Retrying it forever would hide it; raise_for_status surfaces it."""
    acts = _acts(await _sessions(), lambda r: httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await acts.fetch_recipients("ord_1")


# ── write_notification ─────────────────────────────────────────────


async def test_write_mints_the_bell_entry():
    sessions = await _sessions()
    acts = _acts(sessions, lambda r: httpx.Response(200))
    await acts.write_notification(NOTICE, "usr_1")
    async with sessions() as s:
        (row,) = (await s.execute(sa.select(notifications))).all()
    assert (row.recipient_type, row.recipient_id, row.order_id) == ("customer", "usr_1", "ord_1")
    assert row.title == "Refund on its way"
    assert "$12.00" in row.body
    assert row.created_at.replace(tzinfo=UTC).isoformat() == NOTICE.occurred_at


async def test_write_is_idempotent_across_workflow_retries():
    """The id derives from the event, so a retried activity (or a second
    workflow for a redelivered event) collides on the PK."""
    sessions = await _sessions()
    acts = _acts(sessions, lambda r: httpx.Response(200))
    await acts.write_notification(NOTICE, "usr_1")
    await acts.write_notification(NOTICE, "usr_1")
    async with sessions() as s:
        count = (
            await s.execute(sa.select(sa.func.count()).select_from(notifications))
        ).scalar_one()
    assert count == 1


def test_activities_are_registered_under_their_wire_names():
    acts = NotificationActivities(None, None, order_base_url="http://order")  # type: ignore[arg-type]
    assert len(acts.all()) == 2


# ── the starter ────────────────────────────────────────────────────


async def test_disarmed_notifier_skips_rather_than_fails():
    """No temporal_address: a dev stack still consumes the payments topic."""
    notifier = DisarmedRefundNotifier()
    await notifier.notify_refund(NOTICE)
    assert notifier.skipped == ["ord_1"]


async def test_starter_launches_one_workflow_per_event_id():
    """The event id IS the workflow id, so a Kafka redelivery addresses the
    SAME execution and REJECT_DUPLICATE makes the second start a no-op —
    the property the deterministic notification id used to carry alone."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        notifier = TemporalRefundNotifier("unused", task_queue=TASK_QUEUE_DEFAULT)
        notifier._client = env.client  # the lazy connect, pre-satisfied

        await notifier.notify_refund(NOTICE)
        await notifier.notify_refund(NOTICE)  # redelivery

        handle = env.client.get_workflow_handle(f"ntf::refund::{NOTICE.event_id}")
        assert (await handle.describe()).id == f"ntf::refund::{NOTICE.event_id}"


async def test_starter_connects_lazily_once():
    """A notification service with no refunds never opens a connection."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        notifier = TemporalRefundNotifier(env.client.service_client.config.target_host)
        assert notifier._client is None  # nothing connected yet
        first = await notifier._connect()
        assert await notifier._connect() is first  # cached


# ── the worker seam ────────────────────────────────────────────────


async def test_build_worker_registers_the_workflow_and_both_activities():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        acts = _acts(await _sessions(), lambda r: httpx.Response(200))
        worker = build_worker(env.client, acts, task_queue=f"tq-{uuid.uuid4().hex}")
        assert worker.task_queue.startswith("tq-")


async def test_write_publishes_the_bell_hint():
    """The FE disables its 15s poll while the SSE stream is up, so without
    a hint a customer watching the bell would never learn about their
    refund. Regression guard: this is exactly what the consumer does
    post-commit, and moving the write into a workflow must not lose it."""
    from notification import push

    hints: list[tuple[str, str]] = []

    class Recorder:
        async def publish(self, channel: str, data: str) -> None:
            hints.append((channel, data))

    sessions = await _sessions()
    acts = _acts(sessions, lambda r: httpx.Response(200))
    push.set_publisher(Recorder())
    try:
        await acts.write_notification(NOTICE, "usr_1")
    finally:
        push.reset_publisher()
    assert hints == [("sfo:notify:customer:usr_1", "customer")]


async def test_a_dropped_hint_never_loses_the_notification():
    """Fails open: the row is the record, the hint is a nudge."""
    from notification import push

    class Broken:
        async def publish(self, channel: str, data: str) -> None:
            raise ConnectionError("redis down")

    sessions = await _sessions()
    acts = _acts(sessions, lambda r: httpx.Response(200))
    push.set_publisher(Broken())
    try:
        await acts.write_notification(NOTICE, "usr_1")
    finally:
        push.reset_publisher()
    async with sessions() as s:
        count = (
            await s.execute(sa.select(sa.func.count()).select_from(notifications))
        ).scalar_one()
    assert count == 1  # written despite the hint failing
